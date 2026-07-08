"""
Smoke test for the bare credentials-holding layer.

Exercises the full round trip in a throwaway directory:
  bootstrap -> login -> add account -> store encrypted keys -> decrypt -> delete

Requires the `hummingbot` package. Easiest way to run it on Windows is inside
the API image (see bare-features/README.md), e.g.:

    docker run --rm -v ${PWD}:/work -w /work/bare-features/tests `
        --entrypoint python hummingbot/hummingbot-api:latest test_credentials.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials"))

from credential_manager import CredentialManager  # noqa: E402

TEST_PASSWORD = "test-password"
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "credentials", "templates", "master_account")


def main():
    base_path = tempfile.mkdtemp(prefix="bare-credentials-")
    print(f"[1] using temp base path: {base_path}")

    manager = CredentialManager(config_password=TEST_PASSWORD, base_path=base_path)
    manager.bootstrap(template_dir=TEMPLATES)
    assert manager.login(), "login with the freshly stored password must succeed"
    print("[2] bootstrap + password verification OK")

    wrong = CredentialManager(config_password="wrong-password", base_path=base_path)
    assert not wrong.login(), "login with a wrong password must fail"
    print("[3] wrong password correctly rejected")

    manager.add_account("test_account")
    assert "test_account" in manager.list_accounts()
    print("[4] account created from master_account skeleton")

    keys_in = {"binance_api_key": "my-api-key", "binance_api_secret": "my-api-secret"}
    manager.add_credentials("test_account", "binance", keys_in)
    assert "binance.yml" in manager.list_credentials("test_account")
    print("[5] binance keys encrypted and persisted")

    on_disk = open(os.path.join(base_path, "credentials", "test_account", "connectors", "binance.yml"),
                   encoding="utf-8").read()
    assert "my-api-key" not in on_disk and "my-api-secret" not in on_disk, \
        "plaintext keys must never appear in the yml file"
    print("[6] on-disk file contains no plaintext secrets")

    keys_out = manager.get_decrypted_keys("test_account", "binance")
    assert keys_out.get("binance_api_key") == "my-api-key", f"decrypt mismatch: {keys_out}"
    assert keys_out.get("binance_api_secret") == "my-api-secret"
    print("[7] decryption round-trip OK")

    manager.delete_credentials("test_account", "binance")
    assert "binance.yml" not in manager.list_credentials("test_account")
    manager.delete_account("test_account")
    assert "test_account" not in manager.list_accounts()
    print("[8] cleanup OK")

    print("\nALL CREDENTIAL TESTS PASSED")


if __name__ == "__main__":
    main()
