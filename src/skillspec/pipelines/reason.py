"""ReasonStage — node-centric spec generation and reasoning."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from skillspec.config import CONFIG
from skillspec.llm import record_calls
from skillspec.models import DefectGroup, StageStats, Verdict, VerifyKind, empty_by_kind
from skillspec.models.report import NodeProgress, NodeStatus

from .utils import CALLS, DEFECTS, SPECS, read_jsonl
from .node import Node

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from skillspec.config import SkillNamespace
    from skillspec.llm import LLMResponse
    from skillspec.models import Metadata
    from .graph import UnifiedGraph
    from pathlib import Path


def load_defects(path: "Path") -> dict[VerifyKind, list[DefectGroup]]:
    """Read defects.jsonl back into kind-bucketed DefectGroups."""
    by_kind = empty_by_kind()
    for rec in read_jsonl(path):
        group = DefectGroup.model_validate(rec)  # bad kind -> ValidationError -> unreadable artifact
        by_kind[group.kind].append(group)
    return by_kind


class ReasonStage:
    """Reason stage methods (mixed into Pipeline); per-node spec generation and reasoning."""

    if TYPE_CHECKING:  # declared on Pipeline; here for type checkers only
        ns: SkillNamespace
        metadata: Metadata
        unified: UnifiedGraph | None
        loaded_defects: dict[VerifyKind, list[DefectGroup]] | None
        reason_nodes: dict[str, NodeProgress] | None

    async def reason(self) -> None:
        """Gen -> reason per node; the runner handles completed stages."""
        with self._stage("reason") as (_, stats):
            if not CONFIG.spec.enable:
                self._skip(stats, "Reason: disabled by config (spec.enable=false) — skipping spec gen and reasoning.")
                return
            unified = self.unified
            if not unified or not unified.nodes:
                # No graph this session (build failed or empty): do NOT load a prior run's defects —
                # verifying stale candidates would misreport them as this run's. Leave loaded_defects unset.
                self._skip(stats, "Reason: no nodes — skipping (no prior defects loaded for verify).")
                return

            nodes = [n for n in unified.nodes.values() if n.in_scope]
            skipped = len(unified.nodes) - len(nodes)
            if skipped:
                self._info(stats, f"Reason: skipping {skipped} non-analyzable node(s).")
            if not nodes:
                self._warn(stats, f"Reason: all {len(unified.nodes)} node(s) out of scope — nothing to reason over.")
            calls_path = self.ns.artifact(CALLS)
            calls = read_jsonl(calls_path) if self.reason_nodes is not None and calls_path.exists() else []
            run_id = uuid4().hex
            if self.reason_nodes is None:
                self.reason_nodes = {n.unit_id: NodeProgress(status="pending") for n in nodes}
                self.report()
            pending = [n for n in nodes if self.reason_nodes[n.unit_id].status != "completed"]
            self._info(stats, f"Reason: traversing {len(pending)} node(s), restoring {len(nodes) - len(pending)} completed node(s).")
            tasks = [asyncio.create_task(self._analyze_node(n, unified, calls, run_id)) for n in pending]
            try:
                await asyncio.gather(*tasks)
            finally:
                # Drain cancellation before the runner writes the final stage checkpoint.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.ns.write_jsonl(CALLS, calls)
            self._persist_specs(unified)
            self._persist_defects(unified, stats)
            if any(state.status != "completed" for state in self.reason_nodes.values()):
                raise RuntimeError("reason did not complete cleanly; progress saved. Use --resume to continue.")

    def _restore_reason_nodes(self) -> None:
        """Restore only products explicitly committed by each node's state."""
        if self.reason_nodes is None:
            return  # legacy checkpoints without node states restart the whole stage
        unified = self.unified
        if unified is None or set(self.reason_nodes) != {n.unit_id for n in unified.nodes.values() if n.in_scope}:
            raise RuntimeError("Reason node set changed; use a new output.dir. No automatic rerun.")
        needs_specs = {uid for uid, state in self.reason_nodes.items() if state.status in ("reason", "completed")}
        try:
            specs = self._indexed_products(SPECS) if needs_specs else {}
            completed = any(p.status == "completed" for p in self.reason_nodes.values())
            path = self.ns.artifact(DEFECTS)
            by_kind = load_defects(path) if completed and path.exists() else empty_by_kind()
            verdicts = self._committed_verdicts(by_kind)
            restored = {}
            for uid in needs_specs:
                rec = specs[uid]
                progress = self.reason_nodes[uid]
                verdict = None
                if progress.status == "completed":
                    verdict = verdicts[uid] if progress.defect else Verdict(unit_id=uid, decision=False)
                node = Node.model_validate({
                    "unit_id": uid, "context": unified.nodes[uid].context,
                    "expect": rec["expect"], "facts": rec["facts"],
                    "verdict": verdict,
                })
                if not node.spec_complete or any(
                    spec.unit_id != uid for spec in [node.expect, *node.facts.values()]
                ):
                    raise ValueError(f"Incomplete or mismatched spec for {uid}")
                restored[uid] = node
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise RuntimeError("Cannot read committed reason products; restore saved files or use a new output.dir. No automatic rerun.") from exc
        for uid, node in restored.items():
            unified.nodes[uid].expect = node.expect
            unified.nodes[uid].facts = node.facts
            unified.nodes[uid].verdict = node.verdict

    def _committed_verdicts(self, by_kind: dict[VerifyKind, list[DefectGroup]]) -> dict[str, Verdict]:
        """Index by reporter, not defect owner; ignore results without a completed checkpoint."""
        verdicts = {}
        for groups in by_kind.values():
            for group in groups:
                for verdict in group.verdicts:
                    progress = self.reason_nodes.get(verdict.unit_id)
                    if progress is None:
                        raise ValueError(f"Unknown defect reporter {verdict.unit_id}")
                    if progress.status != "completed":
                        continue
                    if not progress.defect or not verdict.decision or verdict.unit_id in verdicts:
                        raise ValueError(f"Conflicting defect judgment for {verdict.unit_id}")
                    verdicts[verdict.unit_id] = verdict
        for uid, progress in self.reason_nodes.items():
            if progress.defect and uid not in verdicts:
                raise ValueError(f"Missing committed defect for {uid}")
        return verdicts

    def _indexed_products(self, name: str) -> dict[str, dict]:
        records = {}
        for rec in read_jsonl(self.ns.artifact(name)):
            uid = rec["unit_id"]
            if uid in records:
                raise ValueError(f"Duplicate unit {uid} in {name}")
            records[uid] = rec
        return records

    def _advance_node(self, uid: str, state: NodeStatus) -> None:
        # All product writes and state commits are synchronous, with no await between them:
        # concurrent node tasks cannot interleave these commits on the event loop.
        previous = self.reason_nodes[uid]
        verdict = self.unified.nodes[uid].verdict if state == "completed" else None
        self.reason_nodes[uid] = NodeProgress(
            status=state, defect=verdict.decision if verdict is not None else None,
        )
        try:
            self.report()
        except BaseException:
            self.reason_nodes[uid] = previous
            raise

    async def _analyze_node(self, node: Node, unified: "UnifiedGraph", calls: list[dict], run_id: str) -> None:
        if self.reason_nodes[node.unit_id].status in ("pending", "spec"):
            self._advance_node(node.unit_id, "spec")
            if not await self._run_node(node, node.spec_gen(), calls, run_id, "spec"):
                return
            self._persist_specs(unified)
            self._advance_node(node.unit_id, "reason")
        node.verdict = None
        if not await self._run_node(node, node.spec_reason(resources=unified.resources), calls, run_id, "reason"):
            return
        self._persist_defects(unified)
        self._advance_node(node.unit_id, "completed")

    async def _run_node(
        self, node: Node, op: "Awaitable[tuple[list[LLMResponse], int]]", calls: list[dict],
        run_id: str, phase: str,
    ) -> bool:
        """One node phase, with telemetry retained even on failure or cancellation."""
        with self._stage(f"{node.unit_id}:{phase}") as (_, stats), record_calls() as recorded:
            try:
                responses, failures = await op
                stats.record_responses(responses)
                complete = node.spec_complete if phase == "spec" else node.verdict is not None
                stats.record_failures(failures or (0 if complete else 1))
                return failures == 0 and complete
            except Exception:
                self._fail(stats, f"Reason: node {node.unit_id} {phase} failed", exc=True)
                return False
            finally:
                calls.extend(
                    {**c.model_dump(by_alias=True),
                     "skillspec": {"unit_id": node.unit_id, "kind": node.kind,
                                   **c.meta, "run_id": run_id, "phase": phase}}
                    for c in recorded
                )
                self.ns.write_jsonl(CALLS, calls)

    def _persist_specs(self, unified: "UnifiedGraph") -> None:
        """Write specs.jsonl (per-node expect + facts)."""
        self.ns.write_jsonl(
            SPECS,
            [n.model_dump(mode="json", include={"unit_id", "expect", "facts"}) for n in unified.nodes.values()],
        )

    def _persist_defects(self, unified: "UnifiedGraph", stats: StageStats | None = None) -> None:
        """Write defects.jsonl — one row per defect group (kind kept for graph-free verify)."""
        groups = unified.candidate_defects()
        self.ns.write_jsonl(DEFECTS, [g.model_dump(mode="json") for g in groups])
        if stats is not None:
            findings = sum(len(g.verdicts) for g in groups)
            group_note = f" ({findings} finding(s) bound onto {len(groups)} unit(s))" if findings > len(groups) else ""
            self._info(stats, f"Reason: {len(groups)} candidate defect(s){group_note}.")

    def _load_defects_for_verify(self) -> None:
        """Load a completed reasoning stage; never infer completion from its files."""
        try:
            by_kind = load_defects(self.ns.artifact(DEFECTS))
            if self.reason_nodes is not None:
                if any(p.status != "completed" for p in self.reason_nodes.values()):
                    raise ValueError("Completed reasoning stage contains unfinished nodes")
                self._committed_verdicts(by_kind)
            self.loaded_defects = by_kind
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Cannot read {DEFECTS} for the completed reasoning stage; no automatic rerun.") from exc
        self._skip(None, "Reason: restored completed stage.")
