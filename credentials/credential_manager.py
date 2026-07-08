"""
Bare credentials-holding implementation, compiled from:

  - services/accounts_service.py
      add_account / delete_account / list_accounts / list_credentials /
      delete_credentials / validate_safe_name (path-traversal guard)
  - services/unified_connector_service.py
      update_connector_keys -> the encrypt-and-persist path only

Everything connector-lifecycle related (starting exchange connectors, balance
polling, database writes) is stripped: this is only the encrypted key-storage
layer, so it can be exercised in tests without network access or a database.

Requires the `hummingbot` package (it provides the ETH-keyfile encryption and
the per-connector config schemas used to validate key names).
"""
import re
from typing import Dict, List

from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.settings import AllConnectorSettings

from file_system import fs_util
from hummingbot_api_config_adapter import HummingbotAPIConfigAdapter
from security import BackendAPISecurity

# Safe single path component names: prevents path traversal via '/', '\' or '..'
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

MASTER_ACCOUNT = "master_account"
ACCOUNT_TEMPLATE_FILES = ["conf_client.yml", "conf_fee_overrides.yml", "hummingbot_logs.yml",
                          ".password_verification"]


def validate_safe_name(name: str, label: str = "name") -> str:
    """Reject names that could escape the credentials directory."""
    if not name or not SAFE_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid {label}: '{name}'. Only letters, numbers, underscores and hyphens are allowed.")
    return name


class CredentialManager:
    """
    Holds encrypted connector credentials on disk, per account, exactly the way
    hummingbot-api does (same file layout, same encryption), minus the live
    connector management.

    Layout under `base_path` (the app uses base_path="bots"):
        credentials/<account>/.password_verification
        credentials/<account>/connectors/<connector>.yml
    """

    def __init__(self, config_password: str, base_path: str = "bots"):
        # fs_util is a module-level singleton created at import time; repoint its base.
        fs_util.base_path = base_path
        self.secrets_manager = ETHKeyFileSecretManger(config_password)

    # ------------------------------------------------------------------ setup

    def bootstrap(self, template_dir: str = None):
        """
        Ensure the master_account skeleton exists. Client/fee/log config files are
        copied from `template_dir` when provided; the password-verification file is
        (re)generated from this manager's password if missing.
        """
        fs_util.create_folder("credentials", MASTER_ACCOUNT)
        fs_util.create_folder(f"credentials/{MASTER_ACCOUNT}", "connectors")
        if template_dir:
            import os
            import shutil
            for file in ACCOUNT_TEMPLATE_FILES:
                src = os.path.join(template_dir, file)
                dest = fs_util._get_full_path(f"credentials/{MASTER_ACCOUNT}/{file}")
                if os.path.exists(src) and not os.path.exists(dest):
                    shutil.copy2(src, dest)
        if BackendAPISecurity.new_password_required():
            BackendAPISecurity.store_password_verification(self.secrets_manager)

    def login(self, account_name: str = MASTER_ACCOUNT) -> bool:
        """Validate the password and decrypt the account's connector keys into memory."""
        validate_safe_name(account_name, "account name")
        return BackendAPISecurity.login_account(account_name=account_name,
                                                secrets_manager=self.secrets_manager)

    # --------------------------------------------------------------- accounts

    @staticmethod
    def list_accounts() -> List[str]:
        return fs_util.list_folders("credentials")

    def add_account(self, account_name: str):
        """Create a new account by cloning the master_account config skeleton."""
        validate_safe_name(account_name, "account name")
        if account_name in self.list_accounts():
            raise ValueError("Account already exists.")
        fs_util.create_folder("credentials", account_name)
        fs_util.create_folder(f"credentials/{account_name}", "connectors")
        for file in ACCOUNT_TEMPLATE_FILES:
            if fs_util.path_exists(f"credentials/{MASTER_ACCOUNT}/{file}"):
                fs_util.copy_file(f"credentials/{MASTER_ACCOUNT}/{file}",
                                  f"credentials/{account_name}/{file}")

    def delete_account(self, account_name: str):
        validate_safe_name(account_name, "account name")
        if account_name == MASTER_ACCOUNT:
            raise ValueError("The master account cannot be deleted.")
        fs_util.delete_folder("credentials", account_name)

    # ------------------------------------------------------------ credentials

    @staticmethod
    def list_credentials(account_name: str) -> List[str]:
        validate_safe_name(account_name, "account name")
        return [file for file in fs_util.list_files(f"credentials/{account_name}/connectors")
                if file.endswith(".yml")]

    def add_credentials(self, account_name: str, connector_name: str, keys: Dict[str, str]):
        """
        Validate, encrypt and persist connector API keys for an account
        (services/unified_connector_service.py::update_connector_keys, storage path only).
        """
        validate_safe_name(account_name, "account name")
        validate_safe_name(connector_name, "connector name")
        if not self.login(account_name):
            raise ValueError(f"Failed to authenticate for {account_name}")

        connector_config = HummingbotAPIConfigAdapter(
            AllConnectorSettings.get_connector_config_keys(connector_name)
        )
        for key, value in keys.items():
            setattr(connector_config, key, value)

        BackendAPISecurity.update_connector_keys(account_name, connector_config)
        BackendAPISecurity.decrypt_all(account_name=account_name)

    def get_decrypted_keys(self, account_name: str, connector_name: str) -> Dict[str, str]:
        """Decrypt and return the stored API keys as plain values (for test assertions)."""
        validate_safe_name(account_name, "account name")
        validate_safe_name(connector_name, "connector name")
        if not self.login(account_name):
            raise ValueError(f"Failed to authenticate for {account_name}")
        keys = BackendAPISecurity.api_keys(connector_name)
        return {k: (v.get_secret_value() if hasattr(v, "get_secret_value") else v)
                for k, v in keys.items()}

    def delete_credentials(self, account_name: str, connector_name: str):
        validate_safe_name(account_name, "account name")
        validate_safe_name(connector_name, "connector name")
        path = f"credentials/{account_name}/connectors/{connector_name}.yml"
        if fs_util.path_exists(path):
            fs_util.delete_file(directory=f"credentials/{account_name}/connectors",
                                file_name=f"{connector_name}.yml")
