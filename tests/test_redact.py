from fusion_ai.redact import REDACTED, contains_secret, is_secret_env_name, redact


def test_known_token_formats_are_redacted():
    samples = [
        "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "AWS AKIAABCDEFGHIJKLMNOP here",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
        "cookie _|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone|_ABC123",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ]
    for s in samples:
        out = redact(s)
        assert REDACTED in out, s
        assert contains_secret(s)


def test_assignments_with_secret_names():
    out = redact('API_KEY = "abc123def456"')
    assert "abc123def456" not in out and out.startswith("API_KEY = ") and REDACTED in out
    assert "hunter22pass" not in redact("password: hunter22pass")
    assert "s3cr3tvalue" not in redact('{"client_secret": "s3cr3tvalue"}')


def test_ordinary_code_is_left_alone():
    code = "tokens = text.split()\nauthor = 'Jane Doe'\npassword_field = form.get(name)\n"
    assert redact(code) == code
    assert not contains_secret("def add(a, b):\n    return a + b\n")


def test_env_names():
    assert is_secret_env_name("OPENAI_API_KEY")
    assert is_secret_env_name("GITHUB_TOKEN")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name("USERPROFILE")
