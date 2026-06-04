from __future__ import annotations

from pydantic import BaseModel, Field


class RepoLocalizerOverviewResponse(BaseModel):
    node_kinds: dict[str, int]
    top_files_by_edges: list[dict[str, object]]
    warnings: list[str]
    stats: dict[str, int]


class RepoLocalizerEntrypointsResponse(BaseModel):
    scripts: list[dict[str, str]]
    targets: list[dict[str, str]]
    workflows: list[dict[str, str]]


class CodeLocalizerFunctionToScriptResponse(BaseModel):
    query: str
    matches: list[dict[str, object]] = Field(default_factory=list)


class CodeLocalizerNeighborhoodResponse(BaseModel):
    upstream: list[dict[str, str]]
    downstream: list[dict[str, str]]
