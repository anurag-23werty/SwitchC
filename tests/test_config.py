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
