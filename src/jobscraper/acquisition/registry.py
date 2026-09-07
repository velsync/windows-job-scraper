"""Adapter registry.

Authority: module 02 sections 8-9. Built-in audited adapters run in-process
and obey the host-owned I/O boundary (they only plan and parse).
"""

from __future__ import annotations

from typing import Callable

from jobscraper.acquisition.adapters import ashby, generic, greenhouse, lever
from jobscraper.acquisition.contracts import AdapterManifest


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[dict], object]] = {}
        self._manifests: dict[tuple[str, str], AdapterManifest] = {}
        self.register_builtin()

    def register(
        self, adapter_id: str, adapter_version: str, factory: Callable[[dict], object], manifest: AdapterManifest
    ) -> None:
        self._factories[adapter_id] = factory
        self._manifests[(adapter_id, adapter_version)] = manifest

    def register_builtin(self) -> None:
        def make_greenhouse(config: dict):
            board = config.get("board") or config.get("org")
            if not board:
                raise ValueError("greenhouse binding config requires 'board'")
            return greenhouse.GreenhouseAdapter(board, base_url=config.get("base_url"))

        def make_lever(config: dict):
            company = config.get("company") or config.get("org")
            if not company:
                raise ValueError("lever binding config requires 'company'")
            return lever.LeverAdapter(company, base_url=config.get("base_url"))

        def make_ashby(config: dict):
            org = config.get("org") or config.get("company")
            if not org:
                raise ValueError("ashby binding config requires 'org'")
            return ashby.AshbyAdapter(org, base_url=config.get("base_url"))

        def make_generic(config: dict):
            from jobscraper.recipes.models import ExtractionRecipe

            recipe = None
            if config.get("recipe"):
                recipe = ExtractionRecipe.from_json(config["recipe"])
            return generic.GenericAdapter(config["entry_url"], recipe=recipe)

        def make_discovery(config: dict):
            return generic.GenericDiscoveryAdapter(config["entry_url"])

        self.register("greenhouse", greenhouse.ADAPTER_VERSION, make_greenhouse, greenhouse.MANIFEST)
        self.register("lever", lever.ADAPTER_VERSION, make_lever, lever.MANIFEST)
        self.register("ashby", ashby.ADAPTER_VERSION, make_ashby, ashby.MANIFEST)
        self.register("generic", generic.ADAPTER_VERSION, make_generic, generic.MANIFEST)
        self.register("generic_discovery", generic.ADAPTER_VERSION, make_discovery, generic.GenericDiscoveryAdapter.manifest)

    def manifest_for(self, adapter_id: str, adapter_version: str) -> AdapterManifest | None:
        return self._manifests.get((adapter_id, adapter_version))

    def build(self, adapter_id: str, config: dict) -> object:
        factory = self._factories.get(adapter_id)
        if factory is None:
            raise KeyError(f"unknown adapter {adapter_id}")
        return factory(config or {})

    def all_manifests(self) -> list[AdapterManifest]:
        return list(self._manifests.values())
