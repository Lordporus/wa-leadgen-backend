"""Legacy entry point retained only to reject unauthenticated replay."""
import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Direct replay is disabled; use the protected tenant API"
    )
    parser.add_argument("receipt_id", type=int)
    parser.parse_args()
    raise SystemExit("Direct replay is disabled; use the protected tenant API")


if __name__ == "__main__":
    main()
