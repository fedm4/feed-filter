#!/usr/bin/env python
"""Measure how fast a Laya server classifies feed items.

Nothing in the plan should fix a batch size or a poll interval by guessing. This prints
the numbers those choices depend on: throughput, latency spread, and what concurrency
does or does not buy.

    uv run scripts/bench_classify.py --n 200

It talks to whatever `FF_LAYA_BASE_URL` points at, through the same client the
application uses, so what it measures is what the application will get.
"""

from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import islice
from typing import Any

import httpx

from feedfilter.laya_client import LayaClient, LayaError, build_client
from feedfilter.settings import Settings

# Questions shaped like the ones E1 will build. Criteria wording is a real lever on the
# answers, but here it only has to be representative: what is being timed is the shape of
# the request -- how many questions ride along with one item -- not the wording.
QUESTIONS: dict[str, dict[str, Any]] = {
    "kind": {
        "type": "choice",
        "instructions": "What kind of piece is this?",
        "criteria": {
            "news": "a factual report of something that happened",
            "analysis": "explains causes, consequences or context",
            "opinion": "argues a position the author holds",
            "promotion": "sells a product, service or event",
        },
    },
    "sensationalism": {
        "type": "score",
        "instructions": "How sensational is the tone?",
        "criteria": ["sober and factual", "mildly slanted", "clickbait", "tabloid outrage"],
    },
    "relevance": {
        "type": "score",
        "instructions": "How relevant is this to a reader following technology and economics?",
        "criteria": ["irrelevant", "marginal", "interesting", "essential"],
    },
    "paywalled": {
        "type": "noul",
        "instructions": "Does this look like a teaser for paywalled content?",
    },
}

# Feed items are not uniform, and latency follows token count, so a corpus of one repeated
# string would flatter the numbers. These vary in language and in length, roughly the way
# a real poll cycle does.
CORPUS: list[dict[str, str]] = [
    {
        "title": "Central bank raises rates by 25 basis points",
        "body": "The central bank raised its benchmark rate by 25 basis points on Thursday, "
        "citing core inflation that has stayed above target for six consecutive months. "
        "Policymakers signalled one further increase before the end of the year.",
    },
    {
        "title": "El paro baja en 24.000 personas en el tercer trimestre",
        "body": "La tasa de desempleo se situo en el 11,2% segun los datos publicados hoy, "
        "cuatro decimas menos que en el trimestre anterior. El sector servicios concentro "
        "la mayor parte de la creacion de empleo.",
    },
    {
        "title": "ESCANDALO: lo que NO quieren que sepas sobre tu factura de luz",
        "body": "Increible pero cierto. Miles de familias estan pagando de mas y nadie dice "
        "nada. Te contamos el truco que las electricas llevan anios ocultando.",
    },
    {
        "title": "A quiet case for boring infrastructure",
        "body": "The most valuable systems are the ones nobody talks about. This piece argues "
        "that the industry's appetite for novelty has a cost that shows up years later, in "
        "maintenance budgets and in the people who leave rather than carry the pager.",
    },
    {
        "title": "Review: the new mesh router that finally fixed my dead zone",
        "body": "We were sent a unit to test. Setup took four minutes and coverage reached the "
        "garage for the first time. Readers of this site get 20% off with the code below.",
    },
    {
        "title": "Investigadores publican un modelo abierto de prediccion meteorologica",
        "body": "El sistema, entrenado con cuarenta anios de reanalisis, iguala la precision de "
        "los modelos operativos a diez dias usando una fraccion del coste computacional. El "
        "codigo y los pesos se han publicado bajo licencia permisiva.",
    },
    {
        "title": "Chip maker warns of softer demand into the fourth quarter",
        "body": "Guidance came in below consensus. Management pointed to inventory correction "
        "among networking customers rather than any change in end demand, and kept the "
        "full-year capital expenditure plan unchanged.",
    },
    {
        "title": "Por que deberiamos dejar de hablar de productividad",
        "body": "Sostengo que la obsesion por medir cada hora ha empobrecido la conversacion "
        "sobre el trabajo. No es que medir este mal; es que hemos confundido lo que se deja "
        "medir con lo que importa.",
    },
]


@dataclass
class Outcome:
    seconds: float
    error: str | None = None


def build_questions(count: int) -> dict[str, dict[str, Any]]:
    if not 1 <= count <= len(QUESTIONS):
        raise SystemExit(f"--questions must be between 1 and {len(QUESTIONS)}")
    return dict(islice(QUESTIONS.items(), count))


def server_info(settings: Settings) -> dict[str, Any]:
    """What the server says about itself, so a result can be read months later."""
    try:
        with build_client(settings) as client:
            return client.get("/health").json()
    except httpx.HTTPError, ValueError:
        return {}


def one(client: LayaClient, state: dict[str, str], questions: dict[str, Any]) -> Outcome:
    started = time.perf_counter()
    try:
        client.predict(state, questions)
    except LayaError as exc:
        return Outcome(time.perf_counter() - started, error=type(exc).__name__)
    return Outcome(time.perf_counter() - started)


def run(
    client: LayaClient, n: int, questions: dict[str, Any], concurrency: int
) -> tuple[list[Outcome], float]:
    states = [CORPUS[i % len(CORPUS)] for i in range(n)]
    started = time.perf_counter()
    if concurrency == 1:
        outcomes = [one(client, state, questions) for state in states]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            outcomes = list(pool.map(lambda s: one(client, s, questions), states))
    return outcomes, time.perf_counter() - started


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Small samples make interpolation a false precision."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1)
    return ordered[index]


def report(outcomes: list[Outcome], wall: float, args: argparse.Namespace, info: dict) -> None:
    ok = [o.seconds for o in outcomes if o.error is None]
    failed = [o for o in outcomes if o.error is not None]

    print()
    print(f"  server      {args.url}")
    print(f"  device      {info.get('device', 'unknown')}   loaded {info.get('loaded', '?')}")
    print(
        f"  items       {len(outcomes)}   questions/item {args.questions}   "
        f"concurrency {args.concurrency}"
    )
    print()
    if not ok:
        print("  every request failed; nothing to measure")
    else:
        print(f"  throughput  {len(ok) / wall:.2f} items/sec   ({wall:.1f}s wall)")
        print(
            f"  latency     p50 {percentile(ok, 0.50) * 1000:.0f} ms   "
            f"p95 {percentile(ok, 0.95) * 1000:.0f} ms   "
            f"p99 {percentile(ok, 0.99) * 1000:.0f} ms"
        )
        print(
            f"              min {min(ok) * 1000:.0f} ms   "
            f"mean {statistics.fmean(ok) * 1000:.0f} ms   "
            f"max {max(ok) * 1000:.0f} ms"
        )
    if failed:
        kinds: dict[str, int] = {}
        for outcome in failed:
            kinds[outcome.error or "?"] = kinds.get(outcome.error or "?", 0) + 1
        print(f"  failures    {len(failed)}   " + ", ".join(f"{k} x{v}" for k, v in kinds.items()))
    print()

    # The reason this script exists: turn throughput into the poll interval it implies.
    if ok:
        rate = len(ok) / wall
        for items in (100, 500, 1000):
            print(f"  {items:>5} items would take {items / rate / 60:6.1f} min")
        print()


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--n", type=int, default=50, help="items to classify (default 50)")
    parser.add_argument(
        "--questions",
        type=int,
        default=3,
        help=f"questions per item, 1-{len(QUESTIONS)} (default 3)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="requests in flight (default 1). Laya serialises inference behind "
        "a single lock and has no batch endpoint, so raising this is "
        "expected to buy nothing -- it is here to be measured, not assumed",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=3,
        help="untimed requests first (default 3). Weights that have been paged "
        "out to swap fault back in on first touch and would otherwise land "
        "in the p99",
    )
    parser.add_argument("--url", default=settings.laya_base_url, help="Laya base URL")
    args = parser.parse_args()

    if args.n < 1 or args.concurrency < 1:
        raise SystemExit("--n and --concurrency must be at least 1")
    questions = build_questions(args.questions)

    settings = Settings(laya_base_url=args.url)
    info = server_info(settings)
    with LayaClient(settings) as client:
        if args.warmup:
            print(f"warming up ({args.warmup} requests) ...", flush=True)
            for i in range(args.warmup):
                outcome = one(client, CORPUS[i % len(CORPUS)], questions)
                if outcome.error:
                    raise SystemExit(
                        f"warmup failed ({outcome.error}); is Laya running at {args.url}?"
                    )
        print(f"classifying {args.n} items ...", flush=True)
        outcomes, wall = run(client, args.n, questions, args.concurrency)

    report(outcomes, wall, args, info)
    return 1 if all(o.error for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
