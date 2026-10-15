"""Check installed optional APIs offline; never load a model or claim inference QA.

Run: uv run --locked --extra ocr python scripts/smoke_docling_configuration.py
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path


def worker():
    from pdf_multitool.core import Limits
    from pdf_multitool.worker import _deny_network, _limits

    _limits(asdict(replace(Limits(), docling_timeout_seconds=60)), "docling")
    sys.addaudithook(_deny_network)
    import easyocr
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # This path does not exist. Construction must not fetch or execute models.
    models = Path.cwd() / "not-provisioned"
    config = PdfPipelineOptions(
        artifacts_path=models, enable_remote_services=False, allow_external_plugins=False
    )
    config.do_ocr = True
    config.ocr_options = EasyOcrOptions(
        download_enabled=False, use_gpu=False, model_storage_directory=str(models / "EasyOcr")
    )
    config.do_table_structure = True
    DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=config)},
    )
    if models.exists() or config.ocr_options.download_enabled:
        raise RuntimeError("Unexpected model provisioning during configuration")
    print(
        json.dumps(
            {
                "easyocr": easyocr.__version__,
                "converter_constructed": True,
                "downloads_enabled": config.ocr_options.download_enabled,
                "remote_services": config.enable_remote_services,
                "external_plugins": config.allow_external_plugins,
                "model_inference": False,
            }
        )
    )


def main():
    if sys.argv[1:] == ["--worker"]:
        worker()
        return
    with tempfile.TemporaryDirectory() as temporary:
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH")
            if key in os.environ
        }
        env.update(
            HOME=temporary,
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_DATASETS_OFFLINE="1",
            OMP_NUM_THREADS="2",
            OPENBLAS_NUM_THREADS="2",
            MKL_NUM_THREADS="2",
            UV_PYTHON_DOWNLOADS="never",
        )
        subprocess.run(  # noqa: S603 -- fixed interpreter and this script, no user input
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            cwd=temporary,
            env=env,
            timeout=90,
            check=True,
        )


if __name__ == "__main__":
    main()
