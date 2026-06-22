"""policy.py — the LEARNABLE decision policy (externalized from config constants).

The knobs that decide *when we take a position* used to be hard-coded in ``config.py``
— only a human could change them. They live here as DATA (``data/policy.json``) so the
learning loop (``scripts/learn_policy.py``) can propose and apply data-driven updates,
with guardrails, instead of the thresholds being frozen forever.

This is the precondition for the system learning on its own: you cannot self-tune a
constant. Defaults below mirror the original config values, so behaviour is unchanged
until the learner (or a human) writes a new ``data/policy.json``.

Every applied change appends to ``changelog`` with its evidence, so policy drift is
auditable and reversible. NOTHING here auto-applies; the learner proposes and only
guardrail-cleared changes land (see scripts/learn_policy.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import config, schemas
from .store import read_json, write_json_atomic as write_json


@dataclass
class Policy:
    # --- position-entry thresholds (the "when do I take a position" knobs) ---
    min_profitable_ev: float = 0.02        # net $/contract floor for an actionable lean
    max_market_disagreement: float = 0.20  # gap vs a liquid market above which (w/o high conf) we don't fade
    hard_gap_ceiling: float = 0.35         # ABSOLUTE gap ceiling: above this, NO lean and the adversarial
                                           # veto is ALWAYS binding — even at high confidence. A ~80pt
                                           # divergence from a liquid market (see the URAN post-mortem) is
                                           # model error or a misread resolution rule, not edge; the high-
                                           # confidence carve-outs must not apply to it.
    conviction_medium_ev: float = 0.05     # EV at/above which conviction = medium
    conviction_high_ev: float = 0.12       # EV at/above which conviction = high
    low_confidence_never_leans: bool = True  # a low-confidence estimate is never an actionable lean
    min_confidence_for_lean: str = "medium"  # the minimum confidence tier an actionable lean needs
                                             # ("low"|"medium"|"high"). Raising this to "high" gates
                                             # medium-confidence leans too — the learnable control for
                                             # the finding that medium-confidence fades lose money.

    # --- adversarial gate authority (learned from the veto track record) ---
    adversarial_veto_binding: bool = True   # a veto downgrades the lean to NONE (vs advisory only)
    adversarial_min_veto_precision: float = 0.0  # learner can require the veto's hist. precision before binding

    # --- crowd-adjust (Halawi claim — measured, not assumed) ---
    crowd_adjust_weight_default: float = 0.30

    # --- provenance ---
    updated_at: str = ""
    version: int = 1
    changelog: list = field(default_factory=list)  # [{at, knob, old, new, reason, evidence}]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Policy":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


def default_policy() -> Policy:
    """A Policy seeded from the original config constants (behaviour-preserving)."""
    return Policy(
        min_profitable_ev=config.MIN_PROFITABLE_EV,
        max_market_disagreement=config.MAX_MARKET_DISAGREEMENT,
        updated_at=schemas.utc_now_iso(),
    )


def load() -> Policy:
    """Load the active policy, or the config-seeded default on first run."""
    d = read_json(config.DATA_DIR / "policy.json")
    return Policy.from_dict(d) if d else default_policy()


def save(p: Policy) -> None:
    p.updated_at = schemas.utc_now_iso()
    write_json(config.DATA_DIR / "policy.json", p.to_dict())


def record_change(p: Policy, knob: str, old: Any, new: Any, reason: str,
                  evidence: dict | None = None) -> None:
    """Apply a single knob change and append it to the changelog (auditable + reversible)."""
    setattr(p, knob, new)
    p.version += 1
    p.changelog.append({
        "at": schemas.utc_now_iso(), "knob": knob, "old": old, "new": new,
        "reason": reason, "evidence": evidence or {},
    })


if __name__ == "__main__":
    # Smoke test: round-trip + change-record, no file writes.
    p = default_policy()
    assert p.min_profitable_ev == config.MIN_PROFITABLE_EV
    p2 = Policy.from_dict(p.to_dict())
    assert p2.max_market_disagreement == p.max_market_disagreement
    record_change(p, "max_market_disagreement", 0.20, 0.15, "test", {"n": 4})
    assert p.max_market_disagreement == 0.15 and p.version == 2 and len(p.changelog) == 1
    print("policy OK")
