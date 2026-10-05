"""FastAPI application exposing the peer-review assignment solver."""
from __future__ import annotations

from fractions import Fraction

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .models import (
    AssignRequest,
    AssignResponse,
    BatchCreateRequest,
    BatchReassignResponse,
    BatchState,
    ConfirmRequest,
    FeasibleResponse,
    HealthResponse,
    InfeasibleResponse,
    ReassignFeasibleResponse,
    ReassignInfeasibleResponse,
    ReassignRequest,
    ReassignResponse,
    VersionConflict,
    WithdrawRequest,
)
from .solver import Manuscript, Reviewer, assign, reassign
from .store import BatchStore

app = FastAPI(
    title="Peer Review Assignment API",
    version="1.2.0",
    description=(
        "同行评审分派服务。匹配分为评审人对稿件各主题评分之和；冲突或总分为 0 的组合不可用，"
        "同一稿件不会重复使用同一评审人。完整分派依次优化：最小化评审人最大负载率 → "
        "最大化总匹配分 → 按 (稿件 ID, 评审人 ID) 取字典序最小。无解时返回最小容量缺口证明。"
        "POST /reassign 在评审人退出后做应急改派：撤销退出者的全部关系、保留已确认关系，"
        "依次优化：保留原关系数最大 → 最小化最大负载率 → 最大化总匹配分 → 字典序最小；"
        "无解时明确返回无解。"
        "POST /batches 把已下发的分派保存为可查询的评审批次（SQLite 持久化，重启后仍可读）："
        "复用 /assign 求得完整分派后才保存，重复批次 ID 或无解均不覆盖记录；"
        "POST /batches/{batch_id}/confirmations 按当前版本登记确认关系；"
        "POST /batches/{batch_id}/reassignments 在评审人退出时复用改派求解，"
        "撤销退出者的关系及其确认、保住其他确认，并把完整改派连同撤销/新增关系发布为下一版本。"
        "确认与发布都须携带读取时的版本号：同一版本上的并发请求仅一个能生效，"
        "旧版本返回 409 及当前版本；改派无解时批次、确认和版本均不变。"
    ),
)

store = BatchStore.from_env()


def get_store() -> BatchStore:
    """The active batch store (module-level so tests can substitute it)."""
    return store


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


# ---------------------------------------------------------------------------
# Persistent review batches
# ---------------------------------------------------------------------------


def _version_conflict(current_version: int) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "版本冲突：请求携带的版本已过期，请重新读取批次后重试",
            "current_version": current_version,
        },
    )


def _get_batch_or_404(batch_id: str) -> dict:
    state = get_store().get(batch_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return state


@app.post(
    "/batches",
    response_model=BatchState,
    responses={
        200: {
            "description": "批次已保存，返回版本 1 的完整状态（输入、分派、确认与版本）。",
            "model": BatchState,
        },
        409: {"description": "批次 ID 已存在，未覆盖原记录。"},
        422: {
            "description": (
                "请求体不合法，或分派无解（detail 中附最小容量缺口证明），批次未保存。"
            )
        },
    },
    tags=["batches"],
    summary="保存已下发的分派为可查询的评审批次",
)
def create_batch(payload: BatchCreateRequest) -> dict:
    if get_store().get(payload.batch_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"批次 {payload.batch_id} 已存在，未覆盖原记录"
        )
    manuscripts, reviewers = _to_domain(payload)
    result = assign(manuscripts, reviewers)
    if result["status"] == "infeasible":
        raise HTTPException(
            status_code=422,
            detail={"message": "分派无解，批次未保存", "proof": result["proof"]},
        )
    state = {
        "manuscripts": [manuscript.model_dump() for manuscript in payload.manuscripts],
        "reviewers": [reviewer.model_dump() for reviewer in payload.reviewers],
        "assignments": result["assignments"],
        "confirmed": [],
        "total_score": result["total_score"],
        "max_load_ratio": result["max_load_ratio"],
        "loads": result["loads"],
    }
    if not get_store().create(payload.batch_id, state):
        # Lost a concurrent create race on the same batch ID.
        raise HTTPException(
            status_code=409, detail=f"批次 {payload.batch_id} 已存在，未覆盖原记录"
        )
    return {"batch_id": payload.batch_id, "version": 1, **state}


@app.get(
    "/batches/{batch_id}",
    response_model=BatchState,
    responses={404: {"description": "批次不存在。"}},
    tags=["batches"],
    summary="查询批次的当前输入、分派、确认与版本",
)
def get_batch(batch_id: str) -> dict:
    return {"batch_id": batch_id, **_get_batch_or_404(batch_id)}


@app.post(
    "/batches/{batch_id}/confirmations",
    response_model=BatchState,
    responses={
        404: {"description": "批次不存在。"},
        409: {
            "model": VersionConflict,
            "description": "版本冲突：响应携带当前版本号，原状态不变。",
        },
        422: {"description": "确认关系重复或不属于当前分派。"},
    },
    tags=["batches"],
    summary="按当前版本登记确认关系",
)
def confirm_relations(batch_id: str, payload: ConfirmRequest):
    current = _get_batch_or_404(batch_id)
    if payload.version != current["version"]:
        return _version_conflict(current["version"])

    assigned = {
        (relation["manuscript_id"], relation["reviewer_id"])
        for relation in current["assignments"]
    }
    new_pairs: set[tuple[str, str]] = set()
    for relation in payload.relations:
        pair = (relation.manuscript_id, relation.reviewer_id)
        if pair in new_pairs:
            raise HTTPException(status_code=422, detail=f"确认关系重复: {pair}")
        if pair not in assigned:
            raise HTTPException(
                status_code=422, detail=f"确认关系 {pair} 不属于当前分派"
            )
        new_pairs.add(pair)

    confirmed = sorted(
        {
            (relation["manuscript_id"], relation["reviewer_id"])
            for relation in current["confirmed"]
        }
        | new_pairs
    )
    new_state = {
        "manuscripts": current["manuscripts"],
        "reviewers": current["reviewers"],
        "assignments": current["assignments"],
        "confirmed": [
            {"manuscript_id": manuscript_id, "reviewer_id": reviewer_id}
            for manuscript_id, reviewer_id in confirmed
        ],
        "total_score": current["total_score"],
        "max_load_ratio": current["max_load_ratio"],
        "loads": current["loads"],
    }
    new_version = get_store().update_if_version(
        batch_id, payload.version, new_state
    )
    if new_version is None:
        # Lost a concurrent compare-and-set race on the same version.
        return _version_conflict(get_store().get(batch_id)["version"])
    return {"batch_id": batch_id, "version": new_version, **new_state}


@app.post(
    "/batches/{batch_id}/reassignments",
    response_model=BatchReassignResponse,
    responses={
        200: {
            "description": (
                "可行时返回发布后的新版本（完整改派连同撤销/新增关系）；"
                "无解时返回 infeasible，批次、确认和版本均不变。"
            ),
            "model": BatchReassignResponse,
        },
        404: {"description": "批次不存在。"},
        409: {
            "model": VersionConflict,
            "description": "版本冲突：响应携带当前版本号，原状态不变。",
        },
        422: {"description": "退出者不在批次评审人列表中。"},
    },
    tags=["batches"],
    summary="评审人退出：改派并发布为下一版本",
)
def withdraw_reviewer(batch_id: str, payload: WithdrawRequest):
    current = _get_batch_or_404(batch_id)
    if payload.version != current["version"]:
        return _version_conflict(current["version"])

    reviewer_ids = {reviewer["id"] for reviewer in current["reviewers"]}
    if payload.withdrawn_reviewer_id not in reviewer_ids:
        raise HTTPException(
            status_code=422,
            detail=f"退出者 {payload.withdrawn_reviewer_id} 不在批次评审人列表中",
        )

    stored_input = AssignRequest(
        manuscripts=current["manuscripts"], reviewers=current["reviewers"]
    )
    manuscripts, reviewers = _to_domain(stored_input)
    original = [
        (relation["manuscript_id"], relation["reviewer_id"])
        for relation in current["assignments"]
    ]
    # 撤销退出者的确认，保住其他确认。
    kept_confirmed = sorted(
        (relation["manuscript_id"], relation["reviewer_id"])
        for relation in current["confirmed"]
        if relation["reviewer_id"] != payload.withdrawn_reviewer_id
    )
    result = reassign(
        manuscripts,
        reviewers,
        original=original,
        withdrawn_id=payload.withdrawn_reviewer_id,
        confirmed=kept_confirmed,
    )
    if result["status"] == "infeasible":
        # 批次、确认和版本均不变。
        return {
            "status": "infeasible",
            "batch_id": batch_id,
            "version": current["version"],
            "reason": result["reason"],
        }

    new_state = {
        "manuscripts": current["manuscripts"],
        "reviewers": current["reviewers"],
        "assignments": result["assignments"],
        "confirmed": [
            {"manuscript_id": manuscript_id, "reviewer_id": reviewer_id}
            for manuscript_id, reviewer_id in kept_confirmed
        ],
        "total_score": result["total_score"],
        "max_load_ratio": result["max_load_ratio"],
        "loads": result["loads"],
    }
    new_version = get_store().update_if_version(
        batch_id, payload.version, new_state
    )
    if new_version is None:
        # Lost a concurrent compare-and-set race on the same version.
        return _version_conflict(get_store().get(batch_id)["version"])
    return {
        "status": "feasible",
        "batch_id": batch_id,
        "version": new_version,
        "revoked": result["revoked"],
        "added": result["added"],
        **new_state,
    }


__all__ = [
    "app",
    "FeasibleResponse",
    "InfeasibleResponse",
    "ReassignFeasibleResponse",
    "ReassignInfeasibleResponse",
]
