import logging
import os
import shutil
import tempfile
import uuid

import instaloader
from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks
from instaloader.exceptions import BadCredentialsException, ConnectionException
from pydantic import BaseModel, Field
from supabase import create_client


load_dotenv()

logger = logging.getLogger(__name__)
router = APIRouter()


class DownloadRequest(BaseModel):
    target_profile: str = Field(min_length=1)
    scraper_username: str = Field(min_length=1)
    scraper_password: str = Field(min_length=1)


def process_download(request: DownloadRequest) -> None:
    temporary_directory: str | None = None
    archive_path: str | None = None

    try:
        target_profile = request.target_profile.strip().lstrip("@")
        if not target_profile:
            logger.error("Perfil alvo vazio após a normalização")
            return

        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        if not supabase_url or not supabase_key:
            logger.error("SUPABASE_URL e SUPABASE_KEY precisam estar configurados")
            return

        temporary_directory = tempfile.mkdtemp(prefix="insta-fast-")
        loader = instaloader.Instaloader()
        loader.dirname_pattern = os.path.join(temporary_directory, "{target}")
        loader.login(request.scraper_username, request.scraper_password)
        loader.download_profile(
            target_profile,
            profile_pic=True,
            posts=True,
            tagged=False,
            igtv=True,
            highlights=True,
            stories=True,
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
    except BadCredentialsException:
        logger.exception("Credenciais inválidas para a conta de scraping")
    except ConnectionException:
        logger.exception("Falha de conexão ou bloqueio durante o scraping")
    except Exception:
        logger.exception("Falha inesperada ao processar o download")
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
    background_tasks.add_task(process_download, request)
    return {
        "status": "processing",
        "message": "Download iniciado em segundo plano.",
    }
