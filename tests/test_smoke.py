import pdf_multitool


def test_main_runs(capsys):
    pdf_multitool.main()
    assert "pdf-multitool" in capsys.readouterr().out
