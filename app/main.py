"""FastAPI application exposing the peer-review assignment solver."""
from __future__ import annotations

from fractions import Fraction

from fastapi import FastAPI

from .models import (
    AssignRequest,
    AssignResponse,
    FeasibleResponse,
    HealthResponse,
    InfeasibleResponse,
    ReassignFeasibleResponse,
    ReassignInfeasibleResponse,
    ReassignRequest,
    ReassignResponse,
)
from .solver import Manuscript, Reviewer, assign, reassign

app = FastAPI(
    title="Peer Review Assignment API",
    version="1.1.0",
    description=(
        "同行评审分派服务。匹配分为评审人对稿件各主题评分之和；冲突或总分为 0 的组合不可用，"
        "同一稿件不会重复使用同一评审人。完整分派依次优化：最小化评审人最大负载率 → "
        "最大化总匹配分 → 按 (稿件 ID, 评审人 ID) 取字典序最小。无解时返回最小容量缺口证明。"
        "POST /reassign 在评审人退出后做应急改派：撤销退出者的全部关系、保留已确认关系，"
        "依次优化：保留原关系数最大 → 最小化最大负载率 → 最大化总匹配分 → 字典序最小；"
        "无解时明确返回无解。"
    ),
)


def _to_domain(payload: AssignRequest):
    manuscripts = [
        Manuscript(
            id=manuscript.id,
            topics=tuple(manuscript.topics),
            required=manuscript.required_reviewers,
        )
        for manuscript in payload.manuscripts
    ]
    reviewers = [
        Reviewer(
            id=reviewer.id,
            scores={topic: Fraction(score) for topic, score in reviewer.scores.items()},
            capacity=reviewer.capacity,
            conflicts=frozenset(reviewer.conflicts),
        )
        for reviewer in payload.reviewers
    ]
    return manuscripts, reviewers


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["meta"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/assign",
    response_model=AssignResponse,
    responses={
        200: {
            "description": "求解完成：可行时返回最优分派，否则返回最小容量缺口证明。",
            "model": AssignResponse,
        },
        422: {"description": "请求体不符合约束（数量范围、ID 唯一性、评分非负等）。"},
    },
    summary="求解同行评审分派",
)
def create_assignment(payload: AssignRequest) -> dict:
    manuscripts, reviewers = _to_domain(payload)
    return assign(manuscripts, reviewers)


@app.post(
    "/reassign",
    response_model=ReassignResponse,
    responses={
        200: {
            "description": "求解完成：可行时返回完整改派及撤销/新增关系，否则明确返回无解。",
            "model": ReassignResponse,
        },
        422: {
            "description": (
                "请求体不合法（原分派不满足稿件需求/冲突/容量约束、"
                "已确认关系不属于原分派或涉及退出者等）。"
            )
        },
    },
    summary="评审人退出后的应急改派",
)
def create_reassignment(payload: ReassignRequest) -> dict:
    manuscripts, reviewers = _to_domain(payload)
    return reassign(
        manuscripts,
        reviewers,
        original=[
            (relation.manuscript_id, relation.reviewer_id)
            for relation in payload.original_assignments
        ],
        withdrawn_id=payload.withdrawn_reviewer_id,
        confirmed=[
            (relation.manuscript_id, relation.reviewer_id)
            for relation in payload.confirmed
        ],
    )


__all__ = [
    "app",
    "FeasibleResponse",
    "InfeasibleResponse",
    "ReassignFeasibleResponse",
    "ReassignInfeasibleResponse",
]
