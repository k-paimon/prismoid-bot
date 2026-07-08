"""
Store Binance API keys into the bare-features encrypted credential store, so
exchange_test_trade.py can use them via --credentials-account instead of env vars.

Needs hummingbot (encryption + connector schemas), so run inside the API image:

    docker run --rm -it -v ${PWD}:/work -w /work/bare-features/poc `
        -e CONFIG_PASSWORD=<your-config-password> `
        -e BINANCE_API_KEY=<key> -e BINANCE_API_SECRET=<secret> `
        --entrypoint python hummingbot/hummingbot-api:latest store_binance_keys.py

Keys land encrypted in <base>/credentials/<account>/connectors/binance.yml.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials"))

from credential_manager import CredentialManager  # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "credentials", "templates", "master_account")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="poc_testnet")
    parser.add_argument("--base-path", default="bots", help="credential store base path")
    args = parser.parse_args()

    password = os.environ.get("CONFIG_PASSWORD")
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if not all([password, api_key, api_secret]):
        sys.exit("Set CONFIG_PASSWORD, BINANCE_API_KEY and BINANCE_API_SECRET env vars first.")

    manager = CredentialManager(config_password=password, base_path=args.base_path)
    manager.bootstrap(template_dir=TEMPLATES)
    if not manager.login():
        sys.exit("CONFIG_PASSWORD does not match the existing password verification file "
                 f"in {args.base_path}/credentials/master_account/.")
    if args.account not in manager.list_accounts():
        manager.add_account(args.account)
    manager.add_credentials(args.account, "binance",
                            {"binance_api_key": api_key, "binance_api_secret": api_secret})
    stored = manager.list_credentials(args.account)
    print(f"Stored encrypted binance keys for account '{args.account}': {stored}")
    roundtrip = manager.get_decrypted_keys(args.account, "binance")
    assert roundtrip["binance_api_key"] == api_key
    print("Decryption round-trip verified.")


if __name__ == "__main__":
    main()
