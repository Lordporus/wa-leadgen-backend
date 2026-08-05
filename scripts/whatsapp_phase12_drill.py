"""Print one provider-disabled Phase 12C operational drill plan."""

import argparse
import json

from app.core.whatsapp_phase12c import DRILL_STEPS, build_offline_drill


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a no-network WhatsApp Phase 12C drill")
    parser.add_argument("scenario", choices=sorted(DRILL_STEPS))
    args = parser.parse_args()
    print(json.dumps(build_offline_drill(args.scenario), indent=2))


if __name__ == "__main__":
    main()
