"""Append-only publisher for verified event batches on a dedicated Git branch.

Only GitHub's Git Data/Contents REST endpoints are used. Updates are ordinary
fast-forward commits; this module never force-pushes or rewrites prior events.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .event_journal import canonical_json, verify_event_batch


class GitHubStateError(RuntimeError):
    """Raised for invalid batches, API errors or concurrent state updates."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PublishResult:
    path: str
    batch_hash: str
    created: bool
    commit_sha: str | None


Transport = Callable[[str, str, Mapping[str, Any] | None], dict[str, Any]]


class GitHubStatePublisher:
    """Publish unique immutable JSON event batches to a GitHub branch."""

    def __init__(
        self,
        repository_full_name: str,
        token: str,
        *,
        branch: str = "state",
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 20.0,
        transport: Transport | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_full_name):
            raise ValueError("repository_full_name must be owner/name")
        if not token.strip():
            raise ValueError("GitHub token is required")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            raise ValueError("Invalid branch name")
        if api_base_url.rstrip("/") != "https://api.github.com":
            raise ValueError("Only the official https://api.github.com host is permitted")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.repository_full_name = repository_full_name
        self.token = token
        self.branch = branch
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubStateError("Only relative GitHub API paths are permitted")
        if self.transport is not None:
            response = self.transport(method, path, payload)
            if not isinstance(response, dict):
                raise GitHubStateError("GitHub transport returned a non-object response")
            return response

        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            f"{self.api_base_url}{path}",
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "trend-quality-paper-state-publisher/0.1",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise GitHubStateError(
                f"GitHub API returned HTTP {exc.code}: {detail[:500]}",
                status_code=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubStateError(f"GitHub API transport failed: {exc}") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubStateError("GitHub API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise GitHubStateError("GitHub API returned a non-object JSON response")
        return decoded

    def _event_path(self, batch: dict[str, Any]) -> str:
        created_at_ms = int(batch["created_at_ms"])
        if created_at_ms <= 0:
            raise GitHubStateError("Batch created_at_ms must be positive")
        date = datetime.fromtimestamp(created_at_ms / 1000, tz=UTC)
        run_id = str(batch["run_id"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", run_id):
            raise GitHubStateError("Unsafe run_id")
        return f"events/{date:%Y}/{date:%m}/{run_id}.json"

    def _latest_batch_hash(
        self,
        repo_path: str,
        tree_sha: str,
        *,
        required_batch_hash: str | None = None,
    ) -> str | None:
        """Verify the full remote event-batch chain and return its unique tip.

        A valid batch hash alone is insufficient: a caller could otherwise
        append a second genesis batch or fork the chain. Read every event file
        from the current tree, verify it, and require one complete linear chain.
        """
        tree = self._request(
            "GET",
            f"{repo_path}/git/trees/{quote(tree_sha, safe='')}?recursive=1",
        )
        nodes = tree.get("tree")
        if tree.get("truncated") is True or not isinstance(nodes, list):
            raise GitHubStateError("State tree is truncated or malformed; refusing to append")

        event_nodes = [
            node
            for node in nodes
            if isinstance(node, dict)
            and node.get("type") == "blob"
            and isinstance(node.get("path"), str)
            and str(node["path"]).startswith("events/")
            and str(node["path"]).endswith(".json")
        ]
        batches: dict[str, dict[str, Any]] = {}
        seen_run_ids: set[str] = set()
        seen_event_ids: set[str] = set()
        seen_idempotency_keys: set[str] = set()
        for node in event_nodes:
            path = str(node["path"])
            blob_sha = node.get("sha")
            if not isinstance(blob_sha, str) or not blob_sha:
                raise GitHubStateError(f"State event tree entry has no blob SHA: {path}")
            blob = self._request(
                "GET",
                f"{repo_path}/git/blobs/{quote(blob_sha, safe='')}",
            )
            if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
                raise GitHubStateError(f"State event blob has unexpected encoding: {path}")
            try:
                decoded = base64.b64decode(blob["content"], validate=False).decode("utf-8")
                batch = json.loads(decoded)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubStateError(f"State event blob cannot be decoded: {path}") from exc
            if not isinstance(batch, dict) or not verify_event_batch(batch):
                raise GitHubStateError(f"State event batch integrity check failed: {path}")
            expected_path = self._event_path(batch)
            if path != expected_path:
                raise GitHubStateError(
                    f"State event path does not match batch metadata: {path} != {expected_path}"
                )
            batch_hash = str(batch["batch_hash"])
            if batch_hash in batches:
                raise GitHubStateError(f"Duplicate event batch hash in state tree: {batch_hash}")
            run_id = str(batch["run_id"])
            if run_id in seen_run_ids:
                raise GitHubStateError(f"Duplicate run_id across remote event batches: {run_id}")
            seen_run_ids.add(run_id)
            for event in batch["events"]:
                event_id = str(event["event_id"])
                idempotency_key = str(event["idempotency_key"])
                if event_id in seen_event_ids:
                    raise GitHubStateError(
                        f"Duplicate event_id across remote event batches: {event_id}"
                    )
                if idempotency_key in seen_idempotency_keys:
                    raise GitHubStateError(
                        "Duplicate idempotency_key across remote event batches: "
                        f"{idempotency_key}"
                    )
                seen_event_ids.add(event_id)
                seen_idempotency_keys.add(idempotency_key)
            batches[batch_hash] = batch

        if not batches:
            if required_batch_hash is not None:
                raise GitHubStateError(
                    f"Required event batch is missing from remote chain: {required_batch_hash}"
                )
            return None

        roots = [
            batch for batch in batches.values()
            if batch.get("previous_batch_hash") is None
        ]
        if len(roots) != 1:
            raise GitHubStateError(
                f"State event chain must have exactly one genesis batch; found {len(roots)}"
            )

        children: dict[str, list[str]] = {}
        for batch_hash, batch in batches.items():
            previous = batch.get("previous_batch_hash")
            if previous is None:
                continue
            if previous not in batches:
                raise GitHubStateError(
                    f"State event chain has an orphan batch: {batch_hash}"
                )
            children.setdefault(str(previous), []).append(batch_hash)

        current_hash = str(roots[0]["batch_hash"])
        visited: set[str] = set()
        while True:
            if current_hash in visited:
                raise GitHubStateError("State event chain contains a cycle")
            visited.add(current_hash)
            next_hashes = children.get(current_hash, [])
            if not next_hashes:
                break
            if len(next_hashes) != 1:
                raise GitHubStateError(
                    f"State event chain forks after batch {current_hash}"
                )
            current_hash = next_hashes[0]

        if len(visited) != len(batches):
            raise GitHubStateError("State event tree contains disconnected batch chains")
        if required_batch_hash is not None and required_batch_hash not in visited:
            raise GitHubStateError(
                f"Published event batch is not in the verified remote chain: {required_batch_hash}"
            )
        return current_hash


    def _verify_remote_batch(
        self,
        repo_path: str,
        branch_path: str,
        encoded_path: str,
        content: str,
        batch_hash: str,
    ) -> None:
        """Verify exact remote contents and full event-chain membership after a write/replay."""
        try:
            verified_ref = self._request(
                "GET",
                f"{repo_path}/git/ref/heads/{branch_path}",
            )
            verified_commit_sha = str(verified_ref["object"]["sha"])
            verified_commit = self._request(
                "GET",
                f"{repo_path}/git/commits/{verified_commit_sha}",
            )
            verified_tree_sha = str(verified_commit["tree"]["sha"])
            self._latest_batch_hash(
                repo_path,
                verified_tree_sha,
                required_batch_hash=batch_hash,
            )
            stored = self._request(
                "GET",
                f"{repo_path}/contents/{encoded_path}?ref={quote(self.branch, safe='')}",
            )
            if stored.get("encoding") != "base64" or not isinstance(
                stored.get("content"), str
            ):
                raise GitHubStateError(
                    "Published event read-back has an unexpected content encoding"
                )
            stored_content = base64.b64decode(
                stored["content"], validate=False
            ).decode("utf-8")
            if stored_content != content:
                raise GitHubStateError(
                    "Published event read-back differs from canonical batch content"
                )
        except GitHubStateError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise GitHubStateError(
                f"Published event read-back verification failed: {type(exc).__name__}"
            ) from exc

    def publish_batch(self, batch: dict[str, Any]) -> PublishResult:
        """Commit a batch as a new immutable file and advance branch fast-forward.

        An identical existing run file is an idempotent success. A different
        payload at the same path is an integrity conflict. A concurrent branch
        update causes a failure requiring a fresh restore/reconcile/retry.
        """
        if not verify_event_batch(batch):
            raise GitHubStateError("Refusing to publish an invalid event batch")
        file_path = self._event_path(batch)
        content = canonical_json(batch) + "\n"
        encoded_path = quote(file_path, safe="/")

        repo_path = f"/repos/{self.repository_full_name}"
        branch_path = quote(self.branch, safe="/")

        # Idempotent duplicate detection before creating any Git objects.
        try:
            existing = self._request(
                "GET",
                f"{repo_path}/contents/{encoded_path}?ref={quote(self.branch, safe='')}",
            )
        except GitHubStateError as exc:
            if exc.status_code != 404:
                raise
            existing = None
        if existing is not None:
            if existing.get("encoding") != "base64" or not isinstance(existing.get("content"), str):
                raise GitHubStateError("Existing event file has an unexpected content encoding")
            try:
                current_content = base64.b64decode(
                    existing["content"], validate=False
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitHubStateError("Existing event file cannot be decoded") from exc
            if current_content != content:
                raise GitHubStateError(
                    f"Immutable event path already exists with different content: {file_path}"
                )
            self._verify_remote_batch(
                repo_path,
                branch_path,
                encoded_path,
                content,
                str(batch["batch_hash"]),
            )
            return PublishResult(
                path=file_path,
                batch_hash=str(batch["batch_hash"]),
                created=False,
                commit_sha=None,
            )

        ref = self._request("GET", f"{repo_path}/git/ref/heads/{branch_path}")
        try:
            parent_sha = str(ref["object"]["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubStateError("State branch ref response is malformed") from exc

        parent = self._request("GET", f"{repo_path}/git/commits/{parent_sha}")
        try:
            base_tree_sha = str(parent["tree"]["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubStateError("Parent commit tree response is malformed") from exc

        latest_batch_hash = self._latest_batch_hash(repo_path, base_tree_sha)
        if batch.get("previous_batch_hash") != latest_batch_hash:
            raise GitHubStateError(
                "Refusing to append event batch with an unanchored previous hash: "
                f"expected {latest_batch_hash!r}, got {batch.get('previous_batch_hash')!r}"
            )

        blob = self._request(
            "POST",
            f"{repo_path}/git/blobs",
            {"content": content, "encoding": "utf-8"},
        )
        try:
            blob_sha = str(blob["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubStateError("GitHub blob response is malformed") from exc

        tree = self._request(
            "POST",
            f"{repo_path}/git/trees",
            {
                "base_tree": base_tree_sha,
                "tree": [
                    {
                        "path": file_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                ],
            },
        )
        try:
            tree_sha = str(tree["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubStateError("GitHub tree response is malformed") from exc

        commit = self._request(
            "POST",
            f"{repo_path}/git/commits",
            {
                "message": f"Append immutable paper event batch {batch['run_id']}",
                "tree": tree_sha,
                "parents": [parent_sha],
            },
        )
        try:
            commit_sha = str(commit["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubStateError("GitHub commit response is malformed") from exc

        try:
            self._request(
                "PATCH",
                f"{repo_path}/git/refs/heads/{branch_path}",
                {"sha": commit_sha, "force": False},
            )
        except GitHubStateError as exc:
            raise GitHubStateError(
                "State branch update was rejected; do not force-push. "
                "Restore the newest state, reconcile idempotency keys, rebuild the batch "
                f"against the latest parent, and retry. Original error: {exc}",
                status_code=exc.status_code,
            ) from exc

        # Confirm exact canonical contents and full-chain membership.
        self._verify_remote_batch(
            repo_path,
            branch_path,
            encoded_path,
            content,
            str(batch["batch_hash"]),
        )

        return PublishResult(
            path=file_path,
            batch_hash=str(batch["batch_hash"]),
            created=True,
            commit_sha=commit_sha,
        )
