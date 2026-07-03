from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from admet.core.api import AdmetAPI
from admet.core.engine import EngineRegistry, RunJob, RunResult, action_spec
from admet.core.project import ProjectStore
from admet.core.run import JsonlRunSink
from admet.core.session import content_cache_key, session_path


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
        self.cache_root = Path(cache_root) if cache_root is not None else Path.home() / ".admet-cache" / "admet2"

    def run(self, targets: list[AnalyzeTarget] | tuple[AnalyzeTarget, ...]) -> AnalyzeBatchReport:
        grouped: dict[Path, list[AnalyzeTarget]] = {}
        for target in targets:
            grouped.setdefault(session_path(target.project_path), []).append(target)

        reports = []
        for project_path, project_targets in grouped.items():
            reports.append(self._run_project(project_path, project_targets))
        return AnalyzeBatchReport(projects=tuple(reports))

    def _run_project(
        self,
        project_path: Path,
        targets: list[AnalyzeTarget],
    ) -> AnalyzeProjectReport:
        store = _open_project(project_path)
        run_target = store.analysis_run_target("analysis")
        job_reports: list[AnalyzeJobReport] = []
        file_ids: list[str] = []
        matrix_rows: list[dict[str, Any]] = []

        with JsonlRunSink(run_target.raw_path) as sink:
            for index, target in enumerate(targets, start=1):
                engine_id = target.engine or infer_engine(target.source_path)
                if engine_id not in self.registry.ids():
                    raise LookupError(f"unknown analyze engine: {engine_id}")
                cache_policy = _cache_policy(target.cache_policy)
                sample_id = target.sample_id or target.source_path.stem
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
                action = action_spec(engine.actions, "analyze")
                action_settings = {
                    key: value
                    for key, value in settings.items()
                    if key in set(action.params) - {"video_path", "input_dir"}
                }
                cache_dir = self._cache_dir(
                    project_id=store.session.project_id,
                    engine_id=engine_id,
                    source_path=target.source_path,
                    settings=action_settings,
                )
                if cache_policy == "discard" and cache_dir.exists():
                    shutil.rmtree(cache_dir)

                job_id = f"{run_target.run_id}_{index}_{engine_id}"
                result = AdmetAPI(engine, session=store.session, workdir=str(store.path)).run(
                    RunJob(
                        id=job_id,
                        engine=engine_id,
                        action="analyze",
                        settings=action_settings,
                        inputs=_job_inputs(engine_id, target.source_path),
                        cache_dir=cache_dir,
                        sink=sink,
                        metadata={
                            "project_id": store.session.project_id,
                            "run_id": run_target.run_id,
                            "sample_id": sample_id,
                            "file_id": file.id,
                            "item_id": sample_id,
                            "cache_policy": cache_policy,
                        },
                    )
                )
                job_reports.append(
                    _job_report(
                        result,
                        sample_id=sample_id,
                        file_id=file.id,
                        source_path=file.path,
                        cache_policy=cache_policy,
                    )
                )

        store.finish_analysis_run(
            run_target,
            files=tuple(file_ids),
            settings={"matrix": matrix_rows},
            metadata={
                "job_count": len(job_reports),
                "jobs": [_job_metadata(job) for job in job_reports],
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
        project_id: str,
        engine_id: str,
        source_path: Path,
        settings: dict[str, Any],
    ) -> Path:
        return self.cache_root / "analysis" / _safe(project_id) / engine_id / _content_key(source_path, settings)


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
    return ProjectStore.create(path, path.stem, "combined")


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
