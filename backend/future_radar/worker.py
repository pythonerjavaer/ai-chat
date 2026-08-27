"""Server-side Future Radar worker entry point.

Examples:
    python -m backend.future_radar.worker --once
    python -m backend.future_radar.worker --once --source legacy-recruitment-pipeline
    python -m backend.future_radar.worker --mock-round 1
"""

from __future__ import annotations

import argparse
import json
import logging

from .. import database
from ..config import settings
from .service import FutureRadarService


def build_service() -> FutureRadarService:
    return FutureRadarService(
        connect=database.connect,
        openai_api_key=settings.openai_api_key,
        ai_model=settings.future_radar_ai_model,
        web_search_enabled=settings.recruitment_web_search_enabled,
        close_confirmations=settings.future_radar_close_confirmations,
        max_workers=settings.future_radar_max_workers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FrostFire Future Radar server worker")
    parser.add_argument("--once", action="store_true", help="run all due sources once")
    parser.add_argument("--source", action="append", default=[], help="run one source ID")
    parser.add_argument("--mock-round", type=int, choices=range(1, 6))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    database.init_db()
    service = build_service()
    service.seed_registry()
    source_ids = list(args.source)
    force = False
    if args.mock_round:
        source = service.repository.get_source("mock-future-radar")
        config = dict(source.get("adapter_config", {})) if source else {"adapter": "mock"}
        config["round"] = args.mock_round
        service.repository.patch_source(
            "mock-future-radar", {"enabled": True, "adapter_config": config}
        )
        source_ids = ["mock-future-radar"]
        force = True
    if not args.once and not source_ids and not args.mock_round:
        parser.error("Choose --once, --source, or --mock-round.")
    run = service.run(
        trigger_type="worker",
        source_ids=source_ids or None,
        force=force,
    )
    logging.info("Future Radar result: %s", json.dumps(run, ensure_ascii=False))
    return 0 if run.get("status") in {"success", "partial_success"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
