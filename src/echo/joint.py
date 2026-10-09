"""Exact legal inventory combinations and conditional joint-upgrade values."""
from __future__ import annotations

from itertools import product
from typing import Iterable

import numpy as np

from src.echo.optimizer import EchoIdentity, NamedEchoBuild, NamedEquippedEcho
from src.echo.simulation import EchoState, EchoStat


ER = EchoStat.ENERGY_REGEN


def echo_key(identity: EchoIdentity, state: EchoState) -> tuple:
    return identity, tuple(sorted((roll.stat.value, roll.tier) for roll in state.rolls))


class JointInventory:
    """Retain completed variants; same-name and main-stat rules remain hard.

    For a fixed ER total the rest of the current scoring profile is linear.
    An envelope of the other slots therefore gives exact candidate gains
    without enumerating every full combination for every Monte Carlo sample.
    """

    def __init__(self, build: NamedEchoBuild,
                 completed: Iterable[NamedEquippedEcho] = ()) -> None:
        self.template = build
        self.items: dict[tuple, NamedEquippedEcho] = {}
        self.revision = 0
        self._envelopes: dict[EchoIdentity, tuple] = {}
        # Equipped inputs may be partial in API tests; scanned additions must
        # be validated by the integration boundary as completed observations.
        for item in (*build.echoes, *completed):
            self.add(item.identity, item.state)

    def compatible(self, identity: EchoIdentity) -> bool:
        return any((slot.identity.cost, slot.identity.compatibility) ==
                   (identity.cost, identity.compatibility) for slot in self.template.echoes)

    def add(self, identity: EchoIdentity, state: EchoState) -> bool:
        key = echo_key(identity, state)
        if not self.compatible(identity) or key in self.items:
            return False
        self.items[key] = NamedEquippedEcho(f'inventory-{len(self.items)}', identity, state)
        self.revision += 1
        self._envelopes.clear()
        return True

    def _pools(self):
        return [[item for item in self.items.values()
                 if (item.identity.cost, item.identity.compatibility) ==
                 (slot.identity.cost, slot.identity.compatibility)]
                for slot in self.template.echoes]

    def optimize(self, current: NamedEchoBuild) -> NamedEchoBuild:
        best = current if current.profile.is_feasible(current.panel) else None
        best_tie = (len(current.echoes), len(current.echoes)) if best else (-1, -1)
        for combo in product(*self._pools()):
            if len({item.identity.name for item in combo}) != len(combo):
                continue
            candidate = NamedEchoBuild(current.profile, current.base_panel, tuple(
                NamedEquippedEcho(slot.slot, item.identity, item.state)
                for slot, item in zip(current.echoes, combo, strict=True)))
            if not candidate.profile.is_feasible(candidate.panel):
                continue
            tie = (
                sum(echo_key(a.identity, a.state) == echo_key(b.identity, b.state)
                    for a, b in zip(current.echoes, candidate.echoes)),
                sum(a.identity.name == b.identity.name
                    for a, b in zip(current.echoes, candidate.echoes)),
            )
            if (best is None or candidate.score > best.score + 1e-9 or
                    (abs(candidate.score-best.score) <= 1e-9 and tie > best_tie)):
                best, best_tie = candidate, tie
        if best is None:
            raise ValueError('No legal joint inventory build satisfies the ER requirement')
        return best

    def _linear(self, panel) -> float:
        return sum(value * self.template.profile.weights[stat]
                   for stat, value in panel.values.items() if stat != ER)

    def is_dominated(self, identity: EchoIdentity, state: EchoState) -> bool:
        """Only same-identity variants may dominate a useful future reserve."""
        panel = self.template.base_panel.add_echo(state)
        score, energy = self._linear(panel), panel.value(ER)
        key = echo_key(identity, state)
        for other_key, item in self.items.items():
            if item.identity != identity or other_key == key:
                continue
            other = self.template.base_panel.add_echo(item.state)
            if self._linear(other) >= score - 1e-9 and other.value(ER) >= energy - 1e-9:
                return True
        return False

    def retained_samples(self, identity, samples, *, required_stats=frozenset()):
        """Estimate full-candidate retention including future-use reserves."""
        profile = self.template.profile
        linear = np.array([sum(r.value * profile.weights[r.stat] for r in state.rolls if r.stat != ER)
                           for state in samples])
        energy = np.array([sum(r.value for r in state.rolls if r.stat == ER) for state in samples])
        retained = np.array([required_stats.issubset(state.stats) for state in samples], dtype=bool)
        for item in self.items.values():
            if item.identity != identity:
                continue
            other_linear = sum(r.value * profile.weights[r.stat] for r in item.state.rolls if r.stat != ER)
            other_energy = sum(r.value for r in item.state.rolls if r.stat == ER)
            retained &= ~((other_linear >= linear-1e-9) & (other_energy >= energy-1e-9))
        return retained

    def _envelope(self, identity: EchoIdentity):
        if identity in self._envelopes:
            return self._envelopes[identity]
        envelope = {}
        pools = self._pools()
        for index, slot in enumerate(self.template.echoes):
            if (slot.identity.cost, slot.identity.compatibility) != (identity.cost, identity.compatibility):
                continue
            for combo in product(*(pool for i, pool in enumerate(pools) if i != index)):
                names = [item.identity.name for item in combo]
                if identity.name in names or len(set(names)) != len(names):
                    continue
                panel = self.template.base_panel
                for item in combo:
                    panel = panel.add_echo(item.state)
                energy = round(panel.value(ER), 8)
                linear = self._linear(panel)
                if energy not in envelope or linear > envelope[energy][0]:
                    envelope[energy] = linear, index
        value = tuple((energy, linear, index) for energy, (linear, index) in envelope.items())
        self._envelopes[identity] = value
        return value

    def gains(self, current: NamedEchoBuild, identity: EchoIdentity,
              samples: tuple[EchoState, ...], *, required_stats=frozenset(),
              minimum_gain: float = 0) -> tuple[np.ndarray, np.ndarray]:
        profile = current.profile
        linear = np.array([sum(r.value * profile.weights[r.stat]
                               for r in state.rolls if r.stat != ER) for state in samples])
        energy = np.array([sum(r.value for r in state.rolls if r.stat == ER) for state in samples])
        valid = np.array([required_stats.issubset(state.stats) for state in samples], dtype=bool)
        gains = np.zeros(len(samples))
        targets = np.full(len(samples), -1, dtype=int)
        policy = profile.energy_policy
        for other_energy, other_linear, index in self._envelope(identity):
            total = energy + other_energy
            if policy:
                value = np.maximum(total, policy.base)
                utility = profile.weights[ER] * (np.minimum(value, policy.required)-policy.base)
                utility += profile.weights[ER] * policy.tolerance_weight_ratio * np.clip(
                    value-policy.required, 0, policy.comfort-policy.required)
                width = policy.overflow-policy.comfort
                if width:
                    distance = np.clip(value-policy.comfort, 0, width)
                    utility += profile.weights[ER] * policy.tolerance_weight_ratio * (
                        distance-distance*distance/(2*width))
                feasible = total + 1e-12 >= policy.required
            else:
                effective = (np.minimum(total, profile.energy_regen_target)
                             if profile.energy_regen_target is not None else total)
                utility = profile.weights[ER] * effective
                feasible = np.ones(len(samples), dtype=bool)
            delta = other_linear + linear + utility-current.score
            improved = valid & feasible & (delta > minimum_gain + 1e-9) & (delta > gains)
            gains = np.where(improved, delta, gains)
            targets = np.where(improved, index, targets)
        return gains, targets
