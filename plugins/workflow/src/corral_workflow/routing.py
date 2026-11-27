"""Three-source capability resolution, 5-class failure taxonomy and shared circuit breaker.

Resolution order (highest wins):
1. Explicit step/CLI pin (``target`` or ``runtime`` + ``model``).
2. Local user overrides (``user_profiles`` / ``user_targets`` in SQLite).
3. Intersection of Cloud Capability Registry rankings with Local Scanned Inventory.
4. Shared ``target_health`` circuit breaker (``closed`` / single-flight ``half_open``).

Fail-closed hard-criteria policy (inspired by LLMPlatform/gateway's
``modelMeetsHardCriteria``): fallback never silently substitutes a candidate
that violates ``exclude_runtimes``, ``require_kind``, ``enabled``, or explicit
``supports_model_override`` constraints.
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any

from corral_workflow.errors import EXIT_NOT_FOUND, EXIT_USAGE, WorkflowError
from corral_workflow.registry import classify_model_family, load_active_registry
from corral_workflow.store import WorkflowStore, compute_version

COST_TIERS = ("free", "low", "medium", "high")
SPEED_TIERS = ("fast", "medium", "slow")
DEGRADATION_POLICIES = ("strict", "pause_on_exhaust", "best_effort")

# 5-class failure taxonomy constants
FAILURE_CALLER = "caller_error"
FAILURE_DETERMINISTIC = "deterministic_unavailable"
FAILURE_HARD_QUOTA = "hard_quota"
FAILURE_TRANSIENT = "transient"
FAILURE_UNCERTAIN = "uncertain"

_CALLER_CODES = frozenset(
    {
        "usage_error",
        "not_found",
        "not_authorized",
        "idempotency_conflict",
        "store_too_new",
        "schema_validation_failed",
    }
)
_DETERMINISTIC_CODES = frozenset(
    {
        "unavailable",
        "executable_missing",
        "launch.executable_missing",
        "unsupported",
        "model_selection_unsupported",
        "auth_expired",
        "missing_login",
        "disabled_by_user",
    }
)
_HARD_QUOTA_CODES = frozenset(
    {
        "quota_exhausted",
        "billing_depleted",
        "account_quota_exceeded",
        "rate_limit_window_exhausted",
    }
)
_UNCERTAIN_CODES = frozenset(
    {
        "delivery_unknown",
        "execution_unknown",
        "mixed_attribution",
        "turn_superseded",
        "waiting_input",
    }
)

_QUOTA_PATTERNS = (
    re.compile(r"accountquotaexceeded", re.I),
    re.compile(r"quota\s+exceeded|insufficient_quota|usage\s+limit|credit\s+balance|billing", re.I),
    re.compile(r"5[- ]hour|rolling\s+window|limit\s+reached|plan\s+limit", re.I),
)
_AUTH_PATTERNS = (
    re.compile(r"not\s+logged\s+in|please\s+login|authentication\s+failed|invalid\s+api\s+key|unauthorized|401", re.I),
    re.compile(r"token\s+expired|re-authenticate|session\s+expired", re.I),
)
_TRANSIENT_PATTERNS = (
    re.compile(r"overloaded_error|service\s+unavailable|bad\s+gateway|gateway\s+timeout|502|503|504|529", re.I),
    re.compile(
        r"connection\s+reset|timed?\s*out|temporarily\s+unavailable|rate\s+limit|too\s+many\s+requests|429", re.I
    ),
)


def classify_failure(
    code: str | None,
    message: str | None = None,
    *,
    http_status: int | None = None,
    execution_state: str | None = None,
    retry_after_s: float | None = None,
) -> dict[str, Any]:
    """Classify a target/step failure into one of the 5 degradation classes."""
    norm_code = (code or "").strip().lower()
    text = str(message or "")

    if execution_state == "unknown" or norm_code in _UNCERTAIN_CODES:
        return {
            "failure_class": FAILURE_UNCERTAIN,
            "abort": False,
            "retry_same_target": False,
            "allow_auto_fallback": False,
            "open_immediately": False,
            "cooldown_seconds": 0.0,
        }

    if norm_code in _CALLER_CODES or (
        http_status is not None and 400 <= http_status < 500 and http_status not in {401, 403, 408, 429}
    ):
        return {
            "failure_class": FAILURE_CALLER,
            "abort": True,
            "retry_same_target": False,
            "allow_auto_fallback": False,
            "open_immediately": False,
            "cooldown_seconds": 0.0,
        }

    if norm_code in _DETERMINISTIC_CODES or http_status in {401, 403} or any(p.search(text) for p in _AUTH_PATTERNS):
        return {
            "failure_class": FAILURE_DETERMINISTIC,
            "abort": False,
            "retry_same_target": False,
            "allow_auto_fallback": True,
            "open_immediately": True,
            "cooldown_seconds": 900.0,
        }

    if norm_code in _HARD_QUOTA_CODES or any(p.search(text) for p in _QUOTA_PATTERNS):
        cooldown = float(retry_after_s) if retry_after_s and retry_after_s > 0 else 900.0
        return {
            "failure_class": FAILURE_HARD_QUOTA,
            "abort": False,
            "retry_same_target": False,
            "allow_auto_fallback": True,
            "open_immediately": True,
            "cooldown_seconds": min(max(cooldown, 60.0), 3600.0),
        }

    if (
        (http_status is not None and (http_status == 429 or http_status == 408 or http_status >= 500))
        or any(p.search(text) for p in _TRANSIENT_PATTERNS)
        or norm_code in {"transient", "timeout", "overloaded", "create_failed", "submit_failed"}
    ):
        cooldown = float(retry_after_s) if retry_after_s and retry_after_s > 0 else 60.0
        return {
            "failure_class": FAILURE_TRANSIENT,
            "abort": False,
            "retry_same_target": True,
            "allow_auto_fallback": True,
            "open_immediately": False,
            "cooldown_seconds": min(max(cooldown, 15.0), 600.0),
        }

    # Unclassified turn failure: treat conservatively as transient-fallbackable without
    # tripping long auth cooldowns.
    return {
        "failure_class": FAILURE_TRANSIENT,
        "abort": False,
        "retry_same_target": True,
        "allow_auto_fallback": True,
        "open_immediately": False,
        "cooldown_seconds": 60.0,
    }


def _dedupe_strs(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_effective_targets(
    store: WorkflowStore,
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge Local Scanned Inventory + Cloud Family Baseline + Local User Overrides."""
    reg_bundle = load_active_registry(store) if manifest is None else {"manifest": manifest}
    active_manifest = reg_bundle["manifest"]
    families = active_manifest.get("families") or {}

    scanned = {item["id"]: item for item in store.list_inventory_targets()}
    user_targets = {item["id"]: item for item in store.list_user_targets()}
    health_map = {h["target_id"]: h for h in store.list_target_health()}

    effective: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(scanned) | set(user_targets))

    for tid in all_ids:
        inv = scanned.get(tid)
        usr = user_targets.get(tid)
        kind = (usr["kind"] if usr else inv["kind"]) if (usr or inv) else "runtime"
        runtime = usr["runtime"] if (usr and usr.get("runtime") is not None) else (inv.get("runtime") if inv else None)
        model = usr["model"] if (usr and usr.get("model") is not None) else (inv.get("model") if inv else None)

        detected_model = None
        if inv and isinstance(inv.get("evidence"), dict):
            detected_model = inv["evidence"].get("detected_default_model")
        family = (
            inv["family"]
            if (inv and inv.get("family") and (usr is None or usr.get("model") is None))
            else classify_model_family(active_manifest, runtime=runtime, model=model or detected_model, kind=kind)
        )
        fam_meta = families.get(family) if isinstance(families.get(family), dict) else {}

        cost_tier = (usr.get("cost_tier") if usr else None) or fam_meta.get("cost_tier") or "medium"
        speed_tier = (usr.get("speed_tier") if usr else None) or fam_meta.get("speed_tier") or "medium"
        base_strengths = list(fam_meta.get("strengths") or [])
        base_avoid = list(fam_meta.get("avoid_for") or [])
        strengths = _dedupe_strs(list(usr["strengths"]) if (usr and usr.get("strengths")) else base_strengths)
        avoid_for = _dedupe_strs(list(usr["avoid_for"]) if (usr and usr.get("avoid_for")) else base_avoid)

        if usr and inv:
            origin = "user_override"
        elif usr:
            origin = usr.get("origin") or "user_custom"
        else:
            origin = "scanned"

        enabled = bool(usr["enabled"]) if usr is not None else True
        available = bool(inv["available"]) if inv is not None else True
        supports_override = bool(inv["supports_model_override"]) if inv is not None else True
        max_concurrency = usr.get("max_concurrency") if usr else None
        notes = usr.get("notes") if usr else ""

        mutable_for_version = {
            "id": tid,
            "origin": origin,
            "kind": kind,
            "runtime": runtime,
            "model": model,
            "cost_tier": cost_tier,
            "speed_tier": speed_tier,
            "strengths": strengths,
            "avoid_for": avoid_for,
            "max_concurrency": max_concurrency,
            "enabled": enabled,
            "notes": notes,
        }
        version = usr["version"] if usr is not None else compute_version(mutable_for_version)
        health = health_map.get(tid) or store.get_target_health(tid)

        effective[tid] = {
            **mutable_for_version,
            "family": family,
            "available": available,
            "supports_model_override": supports_override,
            "auth_hint": inv.get("auth_hint") if inv else "unknown",
            "cli_version": inv.get("cli_version") if inv else None,
            "source_path": inv.get("source_path") if inv else None,
            "scanned_at": inv.get("scanned_at") if inv else None,
            "health": {
                "state": health["state"],
                "cooldown_remaining_s": health["cooldown_remaining_s"],
                "failure_class": health["failure_class"],
                "reason": health["reason"],
            },
            "version": version,
        }

    return effective


def _intersect_cloud_capability(
    cap_id: str,
    cap_spec: dict[str, Any],
    targets: dict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Intersect cloud capability rankings with locally available enabled targets."""
    rankings = cap_spec.get("rankings") if isinstance(cap_spec, dict) else None
    if not isinstance(rankings, list):
        return [], []

    ordered: list[str] = []
    explain: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rank_idx, entry in enumerate(rankings, start=1):
        if not isinstance(entry, dict):
            continue
        fam = entry.get("family")
        rt_hint = entry.get("runtime_hint")
        matches: list[tuple[int, str]] = []
        for tid, target in targets.items():
            if tid in seen or not target.get("enabled") or not target.get("available"):
                continue
            if cap_id in (target.get("avoid_for") or []):
                continue
            if target.get("family") != fam:
                continue
            # Prefer exact runtime_hint match, then explicit model target over generic default.
            hint_score = 0 if (rt_hint and target.get("runtime") == rt_hint) else (1 if not rt_hint else 2)
            specific_score = 0 if target.get("model") else 1
            matches.append((hint_score * 10 + specific_score, tid))
        matches.sort()
        for _, tid in matches:
            seen.add(tid)
            ordered.append(tid)
            explain.append({"target_id": tid, "source": "cloud_rank", "rank": rank_idx, "family": fam})

    # Include any user-tagged targets claiming this capability in strengths that were not yet ranked.
    for tid, target in sorted(targets.items()):
        if tid in seen or not target.get("enabled") or not target.get("available"):
            continue
        if cap_id in (target.get("strengths") or []) and cap_id not in (target.get("avoid_for") or []):
            seen.add(tid)
            ordered.append(tid)
            explain.append({"target_id": tid, "source": "strength_tag", "rank": None, "family": target.get("family")})

    return ordered, explain


def build_effective_profiles(
    store: WorkflowStore,
    *,
    targets: dict[str, dict[str, Any]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build all built-in cloud-derived capabilities overlaid with user profiles."""
    reg_bundle = load_active_registry(store) if manifest is None else {"manifest": manifest}
    active_manifest = reg_bundle["manifest"]
    eff_targets = targets if targets is not None else build_effective_targets(store, manifest=active_manifest)
    cloud_caps = active_manifest.get("capabilities") or {}
    user_profiles = {item["id"]: item for item in store.list_user_profiles()}

    profiles: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(cloud_caps) | set(user_profiles))

    for pid in all_ids:
        cap_spec = cloud_caps.get(pid)
        usr = user_profiles.get(pid)
        if usr is not None:
            origin = "user_override" if cap_spec is not None else (usr.get("origin") or "user_custom")
            description = usr.get("description") or (cap_spec.get("description") if isinstance(cap_spec, dict) else "")
            candidates = list(usr.get("candidates") or [])
            degradation_policy = usr.get("degradation_policy") or "pause_on_exhaust"
            explain = [{"target_id": tid, "source": origin, "rank": i + 1} for i, tid in enumerate(candidates)]
            version = usr["version"]
        else:
            origin = "cloud_derived"
            description = cap_spec.get("description", "") if isinstance(cap_spec, dict) else ""
            candidates, explain = _intersect_cloud_capability(pid, cap_spec, eff_targets)
            degradation_policy = "pause_on_exhaust"
            version = compute_version(
                {
                    "id": pid,
                    "origin": origin,
                    "description": description,
                    "candidates": candidates,
                    "degradation_policy": degradation_policy,
                }
            )

        healthy_candidates = [
            tid
            for tid in candidates
            if tid in eff_targets
            and eff_targets[tid]["enabled"]
            and eff_targets[tid]["available"]
            and eff_targets[tid]["health"]["state"] != "open"
        ]
        profiles[pid] = {
            "id": pid,
            "origin": origin,
            "description": description,
            "candidates": candidates,
            "healthy_candidates": healthy_candidates,
            "degradation_policy": degradation_policy,
            "explain": explain,
            "version": version,
        }

    return profiles


def build_catalog(
    store: WorkflowStore,
    *,
    for_capability: str | None = None,
    available_only: bool = False,
    explain: bool = False,
) -> dict[str, Any]:
    """Assemble the progressive-disclosure capability catalog for authoring agents and humans."""
    reg_bundle = load_active_registry(store)
    manifest = reg_bundle["manifest"]
    targets = build_effective_targets(store, manifest=manifest)
    profiles = build_effective_profiles(store, targets=targets, manifest=manifest)

    if for_capability:
        needle = for_capability.strip().lower()
        filtered_profiles = {
            pid: p
            for pid, p in profiles.items()
            if pid.lower() == needle or needle in pid.lower() or needle in (p.get("description") or "").lower()
        }
        filtered_targets = {
            tid: t
            for tid, t in targets.items()
            if any(tid in p["candidates"] for p in filtered_profiles.values())
            or any(needle == s.lower() for s in (t.get("strengths") or []))
        }
    else:
        filtered_profiles = profiles
        filtered_targets = targets

    if available_only:
        filtered_targets = {tid: t for tid, t in filtered_targets.items() if t.get("enabled") and t.get("available")}

    profile_list = []
    for pid in sorted(filtered_profiles):
        item = dict(filtered_profiles[pid])
        if not explain:
            item.pop("explain", None)
        profile_list.append(item)

    target_list = [filtered_targets[tid] for tid in sorted(filtered_targets)]

    return {
        "registry": {
            "source": reg_bundle.get("source"),
            "registry_version": reg_bundle.get("registry_version"),
            "published_at": reg_bundle.get("published_at"),
            "fetched_at": reg_bundle.get("fetched_at"),
        },
        "inventory_scanned_at": store.latest_inventory_scan_time(),
        "profiles_count": len(profile_list),
        "targets_count": len(target_list),
        "profiles": profile_list,
        "targets": target_list,
    }


def _suggest_ids(needle: str, valid_ids: list[str]) -> list[str]:
    return difflib.get_close_matches(needle, valid_ids, n=5, cutoff=0.45)


def resolve_step_candidates(
    store: WorkflowStore,
    *,
    capability: str | None = None,
    profile: str | None = None,
    target: str | None = None,
    runtime: str | None = None,
    model: str | None = None,
    require_kind: str | None = None,
    exclude_runtimes: tuple[str, ...] | list[str] = (),
    probe_owner: str = "resolver",
    claim_probes: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve an ordered list of qualified executable target specs for one step.

    Returns ``{requested, degradation_policy, candidates, skipped}`` where each
    entry in ``candidates`` passed all hard constraints and circuit-breaker checks.
    Read-only by default (``claim_probes=False``): expired ``open`` circuits are
    marked ``half_open_probe=True`` without mutating SQLite health state. Pass
    ``claim_probes=True`` only when execution is about to consume the probe slot.
    """
    cap_name = capability or profile
    excluded = {str(r).strip() for r in exclude_runtimes if str(r).strip()}
    reg_bundle = load_active_registry(store)
    manifest = reg_bundle["manifest"]
    eff_targets = build_effective_targets(store, manifest=manifest)
    eff_profiles = build_effective_profiles(store, targets=eff_targets, manifest=manifest)

    requested = {
        "capability": cap_name,
        "target": target,
        "runtime": runtime,
        "model": model,
        "exclude_runtimes": sorted(excluded),
        "require_kind": require_kind,
    }

    # Case 1: Explicit runtime/model pin (escape hatch)
    if runtime is not None:
        if runtime in excluded:
            raise WorkflowError(
                "capabilities_unsatisfied",
                f"Requested runtime '{runtime}' is excluded by exclude_runtimes.",
                EXIT_USAGE,
                context={"requested": requested},
            )
        synthetic_id = f"pin:{runtime}:{model or 'default'}"
        inv_match = eff_targets.get(f"{runtime}-default")
        if model and inv_match and not inv_match.get("supports_model_override", True):
            raise WorkflowError(
                "model_selection_unsupported",
                f"Runtime '{runtime}' does not support explicit --model selection on its hosted CLI.",
                EXIT_USAGE,
                hint="Omit model to inherit the runtime's default or choose another runtime.",
                context={"runtime": runtime, "model": model},
            )
        candidate = {
            "target_id": synthetic_id,
            "kind": "runtime",
            "runtime": runtime,
            "model": model,
            "max_concurrency": inv_match.get("max_concurrency") if inv_match else None,
            "fallback_rank": 0,
            "source": "explicit_pin",
        }
        return {
            "requested": requested,
            "degradation_policy": "strict",
            "candidates": [candidate],
            "skipped": [],
        }

    # Case 2: Explicit concrete target pin
    if target is not None:
        if target not in eff_targets:
            valid = sorted(eff_targets)
            raise WorkflowError(
                "not_found",
                f"Target '{target}' not found in workflow catalog.",
                EXIT_NOT_FOUND,
                hint="Run 'corral workflow target list' or 'corral workflow scan' to inspect available targets.",
                context={"available_targets": valid, "similar_ids": _suggest_ids(target, valid)},
            )
        cand_ids = [target]
        degradation_policy = "strict"
        source_label = "explicit_target"
    elif cap_name is not None:
        if cap_name not in eff_profiles:
            valid = sorted(eff_profiles)
            raise WorkflowError(
                "not_found",
                f"Capability/profile '{cap_name}' not found.",
                EXIT_NOT_FOUND,
                hint="Run 'corral workflow catalog' to see built-in capabilities and user profiles.",
                context={"available_profiles": valid, "similar_ids": _suggest_ids(cap_name, valid)},
            )
        prof = eff_profiles[cap_name]
        cand_ids = list(prof["candidates"])
        degradation_policy = prof["degradation_policy"]
        source_label = prof["origin"]
        if degradation_policy == "best_effort":
            for tid, t in sorted(eff_targets.items()):
                if tid not in cand_ids and t.get("enabled") and t.get("available"):
                    cand_ids.append(tid)
    else:
        # Unspecified subject: pick available targets matching require_kind.
        cand_ids = [tid for tid in sorted(eff_targets) if eff_targets[tid]["enabled"] and eff_targets[tid]["available"]]
        degradation_policy = "pause_on_exhaust"
        source_label = "default_pool"

    qualified: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    ts = time.time() if now is None else now

    for rank, tid in enumerate(cand_ids):
        spec = eff_targets.get(tid)
        if spec is None:
            skipped.append({"target_id": tid, "reason": "target_not_found"})
            continue
        if not spec.get("enabled", True):
            skipped.append({"target_id": tid, "reason": "disabled_by_user"})
            continue
        if not spec.get("available", True):
            skipped.append({"target_id": tid, "reason": "unavailable_on_host"})
            continue
        if require_kind and spec.get("kind") != require_kind:
            skipped.append({"target_id": tid, "reason": f"kind_mismatch:{spec.get('kind')}"})
            continue
        if spec.get("runtime") and spec["runtime"] in excluded:
            skipped.append({"target_id": tid, "reason": f"excluded_runtime:{spec['runtime']}"})
            continue
        if spec.get("model") and not spec.get("supports_model_override", True):
            skipped.append({"target_id": tid, "reason": "model_selection_unsupported"})
            continue

        health = store.get_target_health(tid)
        if health["state"] == "open":
            cooldown_until = health.get("cooldown_until")
            cooldown_expired = cooldown_until is None or ts >= float(cooldown_until)
            if not cooldown_expired:
                skipped.append(
                    {
                        "target_id": tid,
                        "reason": f"circuit_open:{health.get('failure_class') or 'unknown'}",
                        "cooldown_remaining_s": max(0.0, round(float(cooldown_until) - ts, 1)),
                    }
                )
                continue
            if claim_probes:
                if store.try_claim_half_open_probe(tid, owner=probe_owner, now=ts):
                    probe = True
                else:
                    skipped.append(
                        {
                            "target_id": tid,
                            "reason": "circuit_half_open_probe_in_flight",
                        }
                    )
                    continue
            else:
                probe = True
        elif health["state"] == "half_open":
            if health.get("half_open_owner") != probe_owner:
                skipped.append({"target_id": tid, "reason": "circuit_half_open_probe_in_flight"})
                continue
            probe = True
        else:
            probe = False

        qualified.append(
            {
                "target_id": tid,
                "kind": spec["kind"],
                "runtime": spec.get("runtime"),
                "model": spec.get("model"),
                "max_concurrency": spec.get("max_concurrency"),
                "fallback_rank": rank,
                "degraded": rank > 0,
                "half_open_probe": probe,
                "source": source_label,
            }
        )

    return {
        "requested": requested,
        "degradation_policy": degradation_policy,
        "candidates": qualified,
        "skipped": skipped,
    }
