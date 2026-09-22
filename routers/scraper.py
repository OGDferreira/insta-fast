import logging
import os
import shutil
import tempfile
import threading
import uuid
from urllib.parse import urlparse

import instaloader
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from instaloader.exceptions import ConnectionException, ProfileNotExistsException
from pydantic import BaseModel, Field
from supabase import create_client


load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter()
jobs: dict[str, dict[str, str | int]] = {}
jobs_lock = threading.Lock()


class DownloadRequest(BaseModel):
    profile_url: str = Field(min_length=1)


def update_job(job_id: str, **values: str | int) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def is_rate_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return "429" in message or "too many requests" in message


def profile_name_from_url(profile_url: str) -> str:
    parsed_url = urlparse(profile_url.strip())
    if parsed_url.scheme not in {"http", "https"} or parsed_url.netloc.lower() not in {
        "instagram.com",
        "www.instagram.com",
    }:
        raise ValueError("Informe uma URL válida do Instagram.")

    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) != 1 or path_parts[0].startswith((".", "_")):
        raise ValueError("A URL deve apontar diretamente para um perfil público.")

    return path_parts[0]


def process_download(job_id: str, profile_url: str) -> None:
    temporary_directory: str | None = None
    archive_path: str | None = None

    try:
        update_job(job_id, status="baixando", progress=10, message="Preparando o download...")
        target_profile = profile_name_from_url(profile_url)

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL e SUPABASE_KEY precisam estar configurados")
            update_job(job_id, status="falhou", progress=0, message="Supabase não está configurado.")
            return

        update_job(
            job_id,
            status="baixando",
            progress=15,
            message="Consultando o perfil público no Instagram...",
        )
        loader = instaloader.Instaloader(
            sleep=False,
            max_connection_attempts=1,
            request_timeout=60,
            fatal_status_codes=[429],
        )
        temporary_directory = tempfile.mkdtemp(prefix="insta-fast-")
        loader.dirname_pattern = os.path.join(temporary_directory, "{target}")
        loader.download_profile(
            target_profile,
            profile_pic=False,
            fast_update=False,
        )
        update_job(
            job_id,
            status="compactando",
            progress=70,
            message="Publicações e legendas baixadas. Criando o arquivo ZIP...",
        )

        archive_base = os.path.join(
            tempfile.gettempdir(), f"insta-fast-{uuid.uuid4().hex}"
        )
        archive_path = shutil.make_archive(
            archive_base,
            "zip",
            root_dir=temporary_directory,
        )

        supabase = create_client(supabase_url, supabase_key)
        update_job(
            job_id,
            status="enviando",
            progress=88,
            message="Enviando o arquivo para o armazenamento seguro...",
        )
        object_path = f"{target_profile}/{uuid.uuid4().hex}.zip"
        with open(archive_path, "rb") as archive_file:
            supabase.storage.from_("downloads").upload(
                object_path,
                archive_file,
                file_options={"content-type": "application/zip", "upsert": "false"},
            )

        logger.info("Download de %s enviado ao Supabase em %s", target_profile, object_path)
        update_job(
            job_id,
            status="concluido",
            progress=100,
            message="Download concluído! O arquivo foi enviado com sucesso.",
        )
    except ValueError as error:
        logger.error("URL de perfil inválida: %s", error)
        update_job(job_id, status="falhou", progress=0, message=str(error))
    except ProfileNotExistsException:
        logger.exception("Perfil público não encontrado: %s", profile_url)
        update_job(
            job_id,
            status="falhou",
            progress=0,
            message="Perfil público não encontrado.",
        )
    except ConnectionException as error:
        if is_rate_limit_error(error):
            logger.error(
                "Instagram aplicou limite de requisições (HTTP 429) para %s",
                profile_url,
            )
            update_job(
                job_id,
                status="falhou",
                progress=0,
                message=(
                    "O Instagram limitou novas consultas temporariamente. "
                    "Aguarde alguns minutos e tente novamente."
                ),
            )
            return

        logger.exception("Falha de conexão ao baixar o perfil: %s", profile_url)
        update_job(
            job_id,
            status="falhou",
            progress=0,
            message="O Instagram bloqueou ou interrompeu a conexão.",
        )
    except Exception:
        logger.exception("Falha inesperada no download de %s", profile_url)
        update_job(
            job_id,
            status="falhou",
            progress=0,
            message="Ocorreu um erro durante o download. Consulte os logs do servidor.",
        )
    finally:
        if archive_path:
            try:
                os.remove(archive_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("Não foi possível remover o arquivo ZIP temporário")

        if temporary_directory:
            try:
                shutil.rmtree(temporary_directory, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("Não foi possível remover o diretório temporário")


@router.post("/api/download")
def download_profile(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    try:
        profile_name_from_url(request.profile_url)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "aguardando",
            "progress": 0,
            "message": "Download colocado na fila.",
        }

    background_tasks.add_task(process_download, job_id, request.profile_url)
    return {
        "status": "processing",
        "message": "Download de publicações e legendas iniciado.",
        "job_id": job_id,
    }


@router.get("/api/download/{job_id}")
def download_status(job_id: str) -> dict[str, str | int]:
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download não encontrado ou expirado.",
        )

    return {"job_id": job_id, **job}
