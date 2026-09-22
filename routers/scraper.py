import logging
import os
import shutil
import tempfile
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


class DownloadRequest(BaseModel):
    profile_url: str = Field(min_length=1)


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


def process_download(profile_url: str) -> None:
    temporary_directory: str | None = None
    archive_path: str | None = None

    try:
        target_profile = profile_name_from_url(profile_url)

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL e SUPABASE_KEY precisam estar configurados")
            return

        loader = instaloader.Instaloader()
        temporary_directory = tempfile.mkdtemp(prefix="insta-fast-")
        loader.dirname_pattern = os.path.join(temporary_directory, "{target}")
        loader.download_profile(
            target_profile,
            profile_pic=False,
            posts=True,
            tagged=False,
            igtv=False,
            highlights=False,
            stories=False,
            fast_update=False,
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
        object_path = f"{target_profile}/{uuid.uuid4().hex}.zip"
        with open(archive_path, "rb") as archive_file:
            supabase.storage.from_("downloads").upload(
                object_path,
                archive_file,
                file_options={"content-type": "application/zip", "upsert": "false"},
            )

        logger.info("Download de %s enviado ao Supabase em %s", target_profile, object_path)
    except ValueError as error:
        logger.error("URL de perfil inválida: %s", error)
    except ProfileNotExistsException:
        logger.exception("Perfil público não encontrado: %s", profile_url)
    except ConnectionException:
        logger.exception("Falha de conexão ao baixar o perfil: %s", profile_url)
    except Exception:
        logger.exception("Falha inesperada no download de %s", profile_url)
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

    background_tasks.add_task(process_download, request.profile_url)
    return {
        "status": "processing",
        "message": "Download de publicações e legendas iniciado em segundo plano.",
    }
