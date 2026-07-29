"""Operator tool: resume one known-failed WhatsApp outbox intent safely."""

import argparse

from app.services.whatsapp_outbox import replay_outbound_intent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent-id", type=int, required=True)
    parser.add_argument("--client-id", type=int, required=True)
    args = parser.parse_args()
    replay_outbound_intent(intent_id=args.intent_id, client_id=args.client_id)
    print(f"resumed intent_id={args.intent_id}")


if __name__ == "__main__":
    main()
