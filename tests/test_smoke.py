from pdf_multitool.cli import parser


def test_cli_parser():
    assert "pdf-multitool" in parser().format_help()
