import logging
import os
import shutil
import tempfile
import uuid

import instaloader
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks
from instaloader.exceptions import ConnectionException
from pydantic import BaseModel, Field
from supabase import create_client


load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter()


class DownloadRequest(BaseModel):
    target_profile: str = Field(min_length=1)
    sessionid: str = Field(min_length=1)


def process_download(target_profile: str, sessionid: str) -> None:
    temporary_directory: str | None = None
    archive_path: str | None = None

    try:
        target_profile = target_profile.strip().lstrip("@")
        if not target_profile:
            logger.error("Perfil alvo vazio após a normalização")
            return

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL e SUPABASE_KEY precisam estar configurados")
            return

        loader = instaloader.Instaloader()
        loader.context._session.cookies.set(
            "sessionid", sessionid, domain=".instagram.com"
        )
        loader.context.is_logged_in = True
        loader.context.username = "conta_cookie"

        temporary_directory = tempfile.mkdtemp(prefix="insta-fast-")
        loader.dirname_pattern = os.path.join(temporary_directory, "{target}")
        loader.download_profile(target_profile, profile_pic=False)

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
    except ConnectionException:
        logger.exception(
            "Falha de conexão ou sessão sessionid inválida ao baixar %s",
            target_profile,
        )
    except Exception:
        logger.exception(
            "Falha inesperada no download com autenticação por cookie para %s",
            target_profile,
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
    background_tasks.add_task(
        process_download,
        request.target_profile,
        request.sessionid,
    )
    return {
        "status": "processing",
        "message": "Download iniciado em segundo plano.",
    }
