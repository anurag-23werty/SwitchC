"""Settings survive the ways a deployment hands them over."""

from khata.config import Settings


def test_values_are_stripped_of_stray_whitespace(monkeypatch):
    # `docker run --env-file` passes "openai/gpt-oss-120b " through verbatim.
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b  ")
    monkeypatch.setenv("SWITCH_REGISTRATION_TOKEN", " tok ")
    s = Settings()
    assert (
        s.groq_model == "openai/gpt-oss-120b" and s.switch_registration_token == "tok"
    )


def test_a_long_running_service_reads_todays_date_each_time(tmp_path):
    from datetime import date

    from khata.tools import Toolbox

    live = Toolbox(None, files_root=tmp_path, payments_dir=tmp_path)  # type: ignore[arg-type]
    assert live.today == date.today()
    fixed = Toolbox(
        None, files_root=tmp_path, payments_dir=tmp_path, today=date(2026, 1, 2)
    )  # type: ignore[arg-type]
    assert fixed.today == date(2026, 1, 2)
