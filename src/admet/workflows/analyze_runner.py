from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from admet.core.engine import EngineRegistry, action_spec
from admet.core.project import ProjectStore
from admet.core.run import JsonlRunSink, RunJob, RunResult
from admet.core.session import content_cache_key, session_path, write_atomic


class AnalysisStop:
    """How running analyses are asked to stop: after the file in hand, or at once.

    A file stopped at once leaves no result.json in its cache, so it reads as not
    done and is simply analysed again on the next run. Files already finished are
    still written into the project either way.
    """

    def __init__(self) -> None:
        self.after_file = threading.Event()
        self.now = threading.Event()

    def requested(self) -> bool:
        return self.after_file.is_set() or self.now.is_set()


class AnalysisStopped(BaseException):
    """Raised from an engine's progress report; BaseException so a broad except in an engine cannot swallow it."""


# Set by the analysis server when it is shut down (Ctrl+C).
SHUTDOWN = AnalysisStop()
_running = 0
_running_lock = threading.Lock()


def running_batches() -> int:
    with _running_lock:
        return _running


VIDEO_SUFFIXES = {".avi", ".mp4", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
CACHE_POLICIES = {"use", "discard", "skip"}


@dataclass(frozen=True)
class AnalyzeTarget:
    project_path: Path
    source_path: Path
    engine: str = ""
    sample_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    cache_policy: str = "use"


@dataclass(frozen=True)
class AnalyzeJobReport:
    job_id: str
    engine: str
    sample_id: str
    file_id: str
    source_path: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalyzeProjectReport:
    project_path: Path
    run_id: str
    raw_path: Path
    jobs: tuple[AnalyzeJobReport, ...]


@dataclass(frozen=True)
class AnalyzeBatchReport:
    projects: tuple[AnalyzeProjectReport, ...]

    @property
    def jobs(self) -> tuple[AnalyzeJobReport, ...]:
        return tuple(job for project in self.projects for job in project.jobs)


class AnalyzeBatchRunner:
    def __init__(
        self,
        registry: EngineRegistry,
        *,
        cache_root: str | Path | None = None,
    ) -> None:
        self.registry = registry
        self.cache_root = Path(cache_root) if cache_root is not None else Path.home() / ".admet-cache"

    def run(
        self,
        targets: list[AnalyzeTarget] | tuple[AnalyzeTarget, ...],
        *,
        on_progress: Callable[[float], None] | None = None,
        on_file_progress: Callable[[float], None] | None = None,
        stop: AnalysisStop | None = None,
    ) -> AnalyzeBatchReport:
        """Run every target; ON_PROGRESS gets the batch percent, ON_FILE_PROGRESS the current file's.

        STOP (by default the server's shutdown request) ends the batch early; see AnalysisStop.
        """
        global _running
        stop = stop or SHUTDOWN
        with _running_lock:
            _running += 1
        try:
            return self._run(targets, on_progress=on_progress, on_file_progress=on_file_progress, stop=stop)
        finally:
            with _running_lock:
                _running -= 1

    def _run(self, targets, *, on_progress, on_file_progress, stop: AnalysisStop) -> AnalyzeBatchReport:
        grouped: dict[Path, list[AnalyzeTarget]] = {}
        for target in targets:
            grouped.setdefault(session_path(target.project_path), []).append(target)

        total = len(targets)
        state = {"index": 0}

        def begin_file() -> Callable[[int, str], None]:
            index = state["index"]
            state["index"] += 1
            if on_progress is not None and total:
                on_progress(index / total * 100.0)
            if on_file_progress is not None:
                on_file_progress(0.0)

            def report(percent: int, message: str = "") -> None:
                if stop.now.is_set():
                    raise AnalysisStopped()
                percent = max(0.0, min(100.0, float(percent)))
                if on_file_progress is not None:
                    on_file_progress(percent)
                if on_progress is not None and total:
                    on_progress((index + percent / 100.0) / total * 100.0)

            return report

        reports = []
        for project_path, project_targets in grouped.items():
            reports.append(self._run_project(project_path, project_targets, begin_file=begin_file, stop=stop))
        if on_progress is not None:
            on_progress(100.0)
        return AnalyzeBatchReport(projects=tuple(reports))

    def _run_project(
        self,
        project_path: Path,
        targets: list[AnalyzeTarget],
        *,
        begin_file: Callable[[], Callable[[int, str], None]] | None = None,
        stop: AnalysisStop | None = None,
    ) -> AnalyzeProjectReport:
        store = _open_project(project_path)
        run_target = store.analysis_run_target("analysis")
        job_reports: list[AnalyzeJobReport] = []
        file_ids: list[str] = []
        matrix_rows: list[dict[str, Any]] = []

        with _RowBuffer() as sink:
            for index, target in enumerate(targets, start=1):
                sample_id = target.sample_id or target.source_path.stem
                if stop is not None and stop.requested():
                    job_reports.append(_stopped_report(run_target.run_id, index, target, sample_id))
                    continue
                file_progress = begin_file() if begin_file is not None else None
                cache_policy = _cache_policy(target.cache_policy)
                try:
                    engine_id = target.engine or infer_engine(target.source_path)
                except ValueError as exc:
                    job_reports.append(
                        _skipped_target_report(
                            run_id=run_target.run_id,
                            index=index,
                            engine=target.engine,
                            sample_id=sample_id,
                            source_path=str(target.source_path),
                            reason=str(exc),
                        )
                    )
                    matrix_rows.append(
                        _skipped_matrix_row(target, engine=target.engine, cache_policy=cache_policy, reason=str(exc))
                    )
                    continue
                if engine_id not in self.registry.ids():
                    reason = f"unknown analyze engine: {engine_id}"
                    job_reports.append(
                        _skipped_target_report(
                            run_id=run_target.run_id,
                            index=index,
                            engine=engine_id,
                            sample_id=sample_id,
                            source_path=str(target.source_path),
                            reason=reason,
                        )
                    )
                    matrix_rows.append(
                        _skipped_matrix_row(target, engine=engine_id, cache_policy=cache_policy, reason=reason)
                    )
                    continue
                file = store.register_analysis_file(
                    target.source_path,
                    engine=engine_id,
                    sample_id=sample_id,
                )
                if file.id not in file_ids:
                    file_ids.append(file.id)

                settings = dict(target.settings)
                matrix_rows.append(
                    {
                        "file_id": file.id,
                        "engine": engine_id,
                        "sample_id": sample_id,
                        "source_path": file.path,
                        "cache_policy": cache_policy,
                        "settings": settings,
                    }
                )
                if cache_policy == "skip":
                    job_reports.append(
                        AnalyzeJobReport(
                            job_id=f"{run_target.run_id}_{index}_{engine_id}",
                            engine=engine_id,
                            sample_id=sample_id,
                            file_id=file.id,
                            source_path=file.path,
                            status="skipped",
                            metadata={"cache_policy": cache_policy},
                        )
                    )
                    continue

                engine = self.registry.create(engine_id)
                action = action_spec(engine.actions, "run_analysis")
                action_settings = {
                    key: value
                    for key, value in settings.items()
                    if key in set(action.params) - {"video_path", "input_dir"}
                }
                cache_dir = self._cache_dir(
                    source_path=target.source_path,
                    settings=action_settings,
                )
                if cache_policy == "discard" and cache_dir.exists():
                    shutil.rmtree(cache_dir)

                job_id = f"{run_target.run_id}_{index}_{engine_id}"
                rows_path = cache_dir / "rows.jsonl"
                result_path = cache_dir / "result.json"
                if cache_policy == "use" and result_path.is_file() and rows_path.is_file():
                    metadata = dict(_read_json(result_path))
                    metadata["cached"] = True
                    _replay_rows(rows_path, sink, job_id=job_id, item_id=sample_id, file_id=file.id)
                    result = RunResult(
                        job_id=job_id,
                        engine=engine_id,
                        action="run_analysis",
                        status="cached",
                        metadata=metadata,
                    )
                else:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with JsonlRunSink(rows_path) as target_sink:
                            result = engine.run(
                                RunJob(
                                    id=job_id,
                                    engine=engine_id,
                                    action="run_analysis",
                                    settings=action_settings,
                                    inputs=_job_inputs(engine_id, target.source_path),
                                    cache_dir=cache_dir,
                                    sink=target_sink,
                                    progress=file_progress,
                                    metadata={
                                        "session_id": store.session.project_id,
                                        "workdir": str(store.path),
                                        "project_id": store.session.project_id,
                                        "run_id": run_target.run_id,
                                        "sample_id": sample_id,
                                        "file_id": file.id,
                                        "item_id": sample_id,
                                        "cache_policy": cache_policy,
                                    },
                                )
                            )
                        _write_json(result_path, dict(result.metadata))
                    except AnalysisStopped:
                        # Stopped mid-file: no result.json, so the cache reads as not done.
                        job_reports.append(_stopped_report(run_target.run_id, index, target, sample_id))
                        continue
                    _replay_rows(rows_path, sink, job_id=job_id, item_id=sample_id, file_id=file.id)
                job_reports.append(
                    _job_report(
                        result,
                        sample_id=sample_id,
                        file_id=file.id,
                        source_path=file.path,
                        cache_policy=cache_policy,
                    )
                )

        touched_ids = {row["file_id"] for row in sink.rows if row.get("file_id")}
        prior = _read_json(run_target.run_metadata_path)
        prior_meta = prior.get("metadata") if isinstance(prior.get("metadata"), dict) else {}
        prior_settings = prior.get("settings") if isinstance(prior.get("settings"), dict) else {}

        kept_rows = [row for row in _read_jsonl(run_target.raw_path) if row.get("file_id") not in touched_ids]
        _write_jsonl(run_target.raw_path, kept_rows + sink.rows)

        merged_jobs = [
            job
            for job in prior_meta.get("jobs", ())
            if isinstance(job, dict) and job.get("file_id") and job.get("file_id") not in touched_ids
        ] + [_job_metadata(job) for job in job_reports if job.file_id in touched_ids]
        merged_matrix = [
            row
            for row in prior_settings.get("matrix", ())
            if isinstance(row, dict) and row.get("file_id") and row.get("file_id") not in touched_ids
        ] + [row for row in matrix_rows if row.get("file_id") in touched_ids]
        merged_files = list(dict.fromkeys([f for f in prior.get("files", ()) if isinstance(f, str)] + file_ids))

        store.finish_analysis_run(
            run_target,
            files=tuple(merged_files),
            settings={"matrix": merged_matrix},
            metadata={
                "job_count": len(merged_jobs),
                "jobs": merged_jobs,
                "cache_root": str(self.cache_root),
            },
        )
        return AnalyzeProjectReport(
            project_path=store.path,
            run_id=run_target.run_id,
            raw_path=run_target.raw_path,
            jobs=tuple(job_reports),
        )

    def _cache_dir(
        self,
        *,
        source_path: Path,
        settings: dict[str, Any],
    ) -> Path:
        cache_settings = {key: settings[key] for key in _CACHE_KEY_FIELDS if key in settings}
        return self.cache_root / _content_key(source_path, cache_settings) / "cache"


def infer_engine(path: str | Path) -> str:
    source = Path(path)
    if source.is_dir():
        return "cellpose"
    suffix = source.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "opencv"
    if suffix in IMAGE_SUFFIXES:
        return "cellpose"
    raise ValueError(f"cannot infer analyze engine for {source}")


def _open_project(project_path: Path) -> ProjectStore:
    path = session_path(project_path)
    if (path / "manifest.json").is_file():
        return ProjectStore(path)
    return ProjectStore.create(path, path.stem)


def _cache_policy(value: str) -> str:
    policy = str(value or "use").strip().lower()
    if policy not in CACHE_POLICIES:
        raise ValueError(f"unknown cache policy: {value!r}")
    return policy


def _job_inputs(engine_id: str, source_path: Path) -> dict[str, Path]:
    if engine_id == "opencv":
        return {"video": source_path}
    if engine_id == "cellpose":
        return {"input_dir": source_path}
    raise LookupError(f"unknown analyze engine: {engine_id}")


def _skipped_matrix_row(
    target: AnalyzeTarget,
    *,
    engine: str,
    cache_policy: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "file_id": "",
        "engine": engine,
        "sample_id": target.sample_id or target.source_path.stem,
        "source_path": str(target.source_path),
        "cache_policy": cache_policy,
        "settings": dict(target.settings),
        "status": "skipped",
        "reason": reason,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(data, default=str) + "\n")


class _RowBuffer:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _RowBuffer:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def write(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    write_atomic(path, "".join(json.dumps(row, default=str) + "\n" for row in rows))


def _replay_rows(rows_path: Path, sink: Any, *, job_id: str, item_id: str, file_id: str) -> None:
    with rows_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                row["job_id"] = job_id
                row["item_id"] = item_id
                row["file_id"] = file_id
                sink.write(row)


_CACHE_KEY_FIELDS = (
    "start_frame",
    "end_frame",
    "max_frames",
    "frame_limit",
    "roi_x",
    "roi_y",
    "roi_width",
    "roi_height",
)


def _content_key(source_path: Path, settings: dict[str, Any]) -> str:
    path = Path(source_path)
    if path.is_file():
        return content_cache_key(path, settings)[:24]
    digest = hashlib.sha256()
    if path.is_dir():
        for file in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(file.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with file.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    else:
        digest.update(str(path).encode("utf-8"))
    digest.update(json.dumps(settings, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()[:24]


def _job_report(
    result: RunResult,
    *,
    sample_id: str,
    file_id: str,
    source_path: str,
    cache_policy: str,
) -> AnalyzeJobReport:
    metadata = dict(result.metadata)
    metadata.setdefault("cache_policy", cache_policy)
    return AnalyzeJobReport(
        job_id=result.job_id,
        engine=result.engine,
        sample_id=sample_id,
        file_id=file_id,
        source_path=source_path,
        status=result.status,
        metadata=metadata,
        warnings=result.warnings,
    )


def _stopped_report(run_id: str, index: int, target: AnalyzeTarget, sample_id: str) -> AnalyzeJobReport:
    engine_id = target.engine or "unknown"
    return AnalyzeJobReport(
        job_id=f"{run_id}_{index}_{_safe(engine_id)}",
        engine=target.engine,
        sample_id=sample_id,
        file_id="",
        source_path=str(target.source_path),
        status="stopped",
        metadata={"reason": "analysis stopped before this file finished; run again to continue"},
    )


def _skipped_target_report(
    *,
    run_id: str,
    index: int,
    engine: str,
    sample_id: str,
    source_path: str,
    reason: str,
) -> AnalyzeJobReport:
    engine_id = engine or "unknown"
    return AnalyzeJobReport(
        job_id=f"{run_id}_{index}_{_safe(engine_id)}",
        engine=engine,
        sample_id=sample_id,
        file_id="",
        source_path=source_path,
        status="skipped",
        metadata={"reason": reason},
        warnings=(reason,),
    )


def _job_metadata(job: AnalyzeJobReport) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "engine": job.engine,
        "sample_id": job.sample_id,
        "file_id": job.file_id,
        "source_path": job.source_path,
        "status": job.status,
        "metadata": job.metadata,
        "warnings": list(job.warnings),
    }


def _safe(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "project"
