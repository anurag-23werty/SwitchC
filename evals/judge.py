"""G-Eval scoring of the Analyst's notes: the one LLM-judged metric.

The judge is DeepEval's G-Eval on a different model from the Analyst (Groq
qwen3.8-27b vs gpt-oss-120b), reached through Khata's own LLM client, so it gets
the same retries, cache and logging. It scores explanations only; it never sees
or grades a money decision.
"""

from __future__ import annotations

import json
import os
import statistics
from typing import Any

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

from deepeval.metrics import GEval  # noqa: E402
from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from khata.config import get_settings  # noqa: E402
from khata.llm import LLM, endpoints_from_settings  # noqa: E402

JUDGE_CHAIN = ("groq-secondary",)
JUDGE_SYSTEM = "You are a careful, strict evaluator. Follow the instructions exactly."


class _Text(BaseModel):
    text: str


class KhataJudge(DeepEvalBaseLLM):
    """DeepEval model backed by Khata's LLM client (structured output, cached)."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm
        super().__init__(model=llm.endpoints[JUDGE_CHAIN[0]].model)

    def load_model(self, *args, **kwargs):
        return self

    def get_model_name(self, *args, **kwargs) -> str:
        return f"groq/{self.llm.endpoints[JUDGE_CHAIN[0]].model}"

    def supports_log_probs(self) -> bool:
        return False

    def supports_structured_outputs(self) -> bool:
        return True

    async def a_generate(
        self, prompt: str, schema: type[BaseModel] | None = None, **kw
    ):
        res = await self.llm.structured(
            schema or _Text,
            system=JUDGE_SYSTEM,
            user=prompt,
            chain=JUDGE_CHAIN,
            agent="judge",
        )
        return res.value if schema else res.value.text

    def generate(self, prompt: str, schema: type[BaseModel] | None = None, **kw):
        raise RuntimeError("use the async judge (a_measure)")


FAITHFUL = (
    "Faithfulness: every number, flag, check result, route and approver mentioned in "
    "the actual output must appear in, or follow directly from, the input facts. A "
    "precedent may be cited only if it is among the knowledge items in the input. "
    "Penalise any invented, mislabelled or misattributed figure or precedent. The "
    "suggested action is advice to the person who decides: suggesting approve for an "
    "invoice that is awaiting approval is not a contradiction."
)
USEFUL = (
    "Usefulness to the approver: the actual output says plainly what the checks found, "
    "why it matters for this invoice, and what the approver should confirm before "
    "deciding. Penalise vagueness, padding, and advice that ignores the flags."
)


def note_text(content: dict[str, Any]) -> str:
    parts = [content["headline"], content["explanation"]]
    parts += [
        f"Precedent {p['invoice_no']}: {p['what_was_decided']}"
        for p in content["precedents"]
    ]
    parts += [f"Check: {q}" for q in content["questions_for_approver"]]
    parts.append(f"Suggested action: {content['suggested_action']}")
    return "\n".join(parts)


async def judge_notes(results: list[dict]) -> dict[str, Any]:
    s = get_settings()
    llm = LLM(endpoints_from_settings(s), cache_dir=s.llm_cache_dir)
    judge = KhataJudge(llm)
    metrics = {
        "faithfulness": GEval(
            name="Faithfulness",
            criteria=FAITHFUL,
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            model=judge,
            threshold=0.7,
        ),
        "usefulness": GEval(
            name="Usefulness",
            criteria=USEFUL,
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            model=judge,
            threshold=0.7,
        ),
    }
    scored = []
    try:
        for r in results:
            for n in r.get("notes") or []:
                if n["kind"] != "analysis" or "facts_seen" not in n["content"]:
                    continue
                case = LLMTestCase(
                    input=json.dumps(n["content"]["facts_seen"], default=str),
                    actual_output=note_text(n["content"]),
                )
                row: dict[str, Any] = {"id": r["id"], "model": n["model"]}
                for key, metric in metrics.items():
                    try:
                        await metric.a_measure(case, _show_indicator=False)
                        row[key] = round(metric.score, 2)
                        row[f"{key}_reason"] = metric.reason
                    except Exception as e:  # a judge failure is reported, not hidden
                        row[key] = None
                        row[f"{key}_reason"] = f"judge failed: {type(e).__name__}: {e}"[
                            :300
                        ]
                scored.append(row)
    finally:
        await llm.aclose()

    def avg(key: str) -> float | None:
        vals = [x[key] for x in scored if x.get(key) is not None]
        return round(statistics.mean(vals), 2) if vals else None

    def passed(key: str) -> int:
        return sum(1 for x in scored if (x.get(key) or 0) >= 0.7)

    return {
        "summary": {
            "notes": len(scored),
            "judge": judge.get_model_name(),
            "faithfulness_avg": avg("faithfulness"),
            "faithfulness_pass": passed("faithfulness"),
            "usefulness_avg": avg("usefulness"),
            "usefulness_pass": passed("usefulness"),
            "lowest": sorted(
                (x for x in scored if x.get("faithfulness") is not None),
                key=lambda x: x["faithfulness"],
            )[:3],
        },
        "rows": scored,
    }
