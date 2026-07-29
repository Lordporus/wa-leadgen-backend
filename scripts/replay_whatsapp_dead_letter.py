"""Replay one inspected WhatsApp dead-letter receipt by numeric receipt id."""
import argparse

from app.services.whatsapp_queue import replay_dead_letter


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay an inspected WhatsApp dead-letter event")
    parser.add_argument("receipt_id", type=int)
    args = parser.parse_args()
    correlation_id = replay_dead_letter(receipt_id=args.receipt_id)
    print(f"replayed correlation_id={correlation_id}")


if __name__ == "__main__":
    main()
