"""
rules.py - Bloques lógicos estilo App Inventor: condiciones, reglas, procesos y loops.

Jerarquía
    Flow ─┬─ zonas (RelativeZone) y plantillas (TemplateRef)
          └─ Loop 1 (N iteraciones o continuo, delay entre iteraciones)
               └─ Proceso 1
                    └─ Regla: SI <condición> ==> acciones (con delay entrópico [x, y])

Condiciones
    Hoja:      Zona == Baseline   |   Zona == Plantilla
    Operadores: Y / AND,  O / OR,  NOT / NO      (precedencia NOT > Y > O)

    En JSON:
        {"op": "Y", "args": [
            {"zona": "Zona 1", "es": "baseline"},
            {"zona": "Zona 2", "es": "Plantilla B", "umbral": 0.9}
        ]}
    En texto (``parse_expression``):
        SI (Zona 1 == Baseline) Y (Zona 2 == "Plantilla B")

Disparo por flanco (Edge Triggering)
    Por defecto una regla sólo se dispara en la transición FALSO -> VERDADERO.
    Mientras la condición siga siendo verdadera no se repite, lo que evita
    bucles infinitos (p. ej. hacer clic una y otra vez sobre el mismo botón).
    Para volver a disparar, la condición debe pasar antes por FALSO.

Módulo puro (sin OpenCV ni E/S): el motor construye las ``ZoneObservation``
y este módulo sólo decide.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.geometry import RelativeZone

__all__ = [
    "BASELINE", "ZoneObservation", "Condition", "ZoneIs", "And", "Or", "Not",
    "parse_condition", "parse_expression", "condition_to_text", "ConditionSyntaxError",
    "Trigger", "Rule", "Process", "Loop", "TemplateRef", "Flow",
]

BASELINE = "baseline"
_BASELINE_ALIASES = {"baseline", "zona -1", "zona-1", "-1", "base", "neutro"}

Observations = Mapping[str, "ZoneObservation"]


def normalize_target(target: str) -> str:
    """Unifica los alias del estado neutro ('Zona -1', 'Base'...) como ``BASELINE``."""
    t = str(target).strip()
    if not t:
        raise ValueError("Objetivo de condición vacío")
    return BASELINE if t.casefold() in _BASELINE_ALIASES else t


# --------------------------------------------------------------------------- #
# Observación de una zona (la produce el motor en cada ciclo)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ZoneObservation:
    """Lo que el motor "vio" en una zona durante un ciclo.

    ``baseline_delta``: 0.0 = idéntico al Baseline, 1.0 = totalmente distinto.
    ``scores``: plantilla -> similitud TM_CCOEFF_NORMED (-1.0 a 1.0).
    ``locations``: plantilla -> (x, y, w, h) del mejor match en píxeles absolutos.
    """

    zone: str
    baseline_delta: float | None = None
    is_baseline: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    locations: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zona": self.zone,
            "delta": None if self.baseline_delta is None else round(self.baseline_delta, 4),
            "baseline": self.is_baseline,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
        }


# --------------------------------------------------------------------------- #
# Condiciones
# --------------------------------------------------------------------------- #
class Condition(ABC):
    """Nodo del árbol booleano."""

    @abstractmethod
    def evaluate(self, obs: Observations, default_threshold: float) -> bool: ...

    @abstractmethod
    def requirements(self) -> Iterator[tuple[str, str]]:
        """Pares (zona, objetivo) que el motor debe medir para evaluar el nodo."""

    @abstractmethod
    def to_dict(self) -> dict[str, Any]: ...

    def __and__(self, other: Condition) -> And:
        return And((self, other))

    def __or__(self, other: Condition) -> Or:
        return Or((self, other))

    def __invert__(self) -> Not:
        return Not(self)


@dataclass(frozen=True, slots=True)
class ZoneIs(Condition):
    """Hoja: ``zona == baseline`` o ``zona == plantilla``.

    Para plantillas, ``threshold`` (0-1) sobrescribe el umbral global de similitud.
    Si la zona no se pudo observar (ventana fuera de pantalla, etc.) es FALSO.
    """

    zone: str
    target: str
    threshold: float | None = None

    def __post_init__(self) -> None:
        if not self.zone.strip():
            raise ValueError("Condición sin zona")
        object.__setattr__(self, "target", normalize_target(self.target))
        if self.threshold is not None and not -1.0 <= self.threshold <= 1.0:
            raise ValueError(f"Umbral fuera de rango: {self.threshold}")

    def evaluate(self, obs: Observations, default_threshold: float) -> bool:
        o = obs.get(self.zone)
        if o is None:
            return False
        if self.target == BASELINE:
            return o.is_baseline
        score = o.scores.get(self.target)
        umbral = self.threshold if self.threshold is not None else default_threshold
        return score is not None and score >= umbral

    def requirements(self) -> Iterator[tuple[str, str]]:
        yield self.zone, self.target

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"zona": self.zone, "es": self.target}
        if self.threshold is not None:
            data["umbral"] = self.threshold
        return data


@dataclass(frozen=True, slots=True)
class And(Condition):
    items: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("Operador Y sin argumentos")

    def evaluate(self, obs: Observations, default_threshold: float) -> bool:
        return all(c.evaluate(obs, default_threshold) for c in self.items)

    def requirements(self) -> Iterator[tuple[str, str]]:
        for c in self.items:
            yield from c.requirements()

    def to_dict(self) -> dict[str, Any]:
        return {"op": "Y", "args": [c.to_dict() for c in self.items]}


@dataclass(frozen=True, slots=True)
class Or(Condition):
    items: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("Operador O sin argumentos")

    def evaluate(self, obs: Observations, default_threshold: float) -> bool:
        return any(c.evaluate(obs, default_threshold) for c in self.items)

    def requirements(self) -> Iterator[tuple[str, str]]:
        for c in self.items:
            yield from c.requirements()

    def to_dict(self) -> dict[str, Any]:
        return {"op": "O", "args": [c.to_dict() for c in self.items]}


@dataclass(frozen=True, slots=True)
class Not(Condition):
    item: Condition

    def evaluate(self, obs: Observations, default_threshold: float) -> bool:
        return not self.item.evaluate(obs, default_threshold)

    def requirements(self) -> Iterator[tuple[str, str]]:
        yield from self.item.requirements()

    def to_dict(self) -> dict[str, Any]:
        return {"op": "NOT", "arg": self.item.to_dict()}


_OPS_AND = {"y", "and", "&&"}
_OPS_OR = {"o", "or", "||"}
_OPS_NOT = {"not", "no", "!"}


def parse_condition(data: Any) -> Condition:
    """Reconstruye una condición desde JSON (dict) o desde texto (str)."""
    if isinstance(data, Condition):
        return data
    if isinstance(data, str):
        return parse_expression(data)
    if not isinstance(data, Mapping):
        raise ValueError(f"Condición inválida: {data!r}")

    if "op" in data:
        op = str(data["op"]).strip().casefold()
        if op in _OPS_NOT:
            arg = data.get("arg", data.get("args"))
            if isinstance(arg, list):
                if len(arg) != 1:
                    raise ValueError("NOT admite un único argumento")
                arg = arg[0]
            return Not(parse_condition(arg))
        args = tuple(parse_condition(a) for a in data.get("args", ()))
        if op in _OPS_AND:
            return And(args)
        if op in _OPS_OR:
            return Or(args)
        raise ValueError(f"Operador desconocido: {data['op']!r}")

    zona = data.get("zona", data.get("zone"))
    if zona is None:
        raise ValueError(f"Condición sin 'zona': {dict(data)!r}")
    umbral = data.get("umbral", data.get("threshold"))
    umbral = None if umbral is None else float(umbral)
    if "no_es" in data:
        return Not(ZoneIs(str(zona), str(data["no_es"]), umbral))
    objetivo = data.get("es", data.get("is"))
    if objetivo is None:
        raise ValueError(f"Condición sin 'es': {dict(data)!r}")
    return ZoneIs(str(zona), str(objetivo), umbral)


# ------------------------- parser de texto ------------------------------ #
class ConditionSyntaxError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<str>"[^"]*"|'[^']*')          # nombre entre comillas
      | (?P<sym>==|!=|\(|\)|&&|\|\|)      # símbolos
      | (?P<word>[^\s()"'=!&|]+)          # palabra suelta
    )""",
    re.VERBOSE,
)
_KEYWORDS = _OPS_AND | _OPS_OR | _OPS_NOT | {"si", "if"}


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ConditionSyntaxError(f"Carácter inesperado en la posición {pos}: {text[pos:]!r}")
        pos = m.end()
        kind = m.lastgroup or ""
        value = m.group(kind)
        if kind == "word" and value.casefold() in _KEYWORDS:
            kind = "kw"
        elif kind == "sym" and value in ("&&", "||"):
            kind = "kw"
        tokens.append((kind, value))
    return tokens


class _Parser:
    """Descenso recursivo:  expr := term (O term)* ;  term := factor (Y factor)* ;
    factor := NOT factor | '(' expr ')' | nombre ('=='|'!=') nombre"""

    def __init__(self, text: str) -> None:
        self.tokens = _tokenize(text)
        self.i = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise ConditionSyntaxError("Expresión incompleta")
        self.i += 1
        return tok

    def _is_kw(self, group: set[str]) -> bool:
        tok = self._peek()
        return tok is not None and tok[0] == "kw" and tok[1].casefold() in group

    def parse(self) -> Condition:
        if self._is_kw({"si", "if"}):
            self.i += 1
        cond = self._expr()
        if self._peek() is not None:
            raise ConditionSyntaxError(f"Sobra texto a partir de {self._peek()[1]!r}")
        return cond

    def _expr(self) -> Condition:
        items = [self._term()]
        while self._is_kw(_OPS_OR):
            self.i += 1
            items.append(self._term())
        return items[0] if len(items) == 1 else Or(tuple(items))

    def _term(self) -> Condition:
        items = [self._factor()]
        while self._is_kw(_OPS_AND):
            self.i += 1
            items.append(self._factor())
        return items[0] if len(items) == 1 else And(tuple(items))

    def _factor(self) -> Condition:
        if self._is_kw(_OPS_NOT):
            self.i += 1
            return Not(self._factor())
        tok = self._peek()
        if tok == ("sym", "("):
            self.i += 1
            cond = self._expr()
            if self._next() != ("sym", ")"):
                raise ConditionSyntaxError("Falta ')'")
            return cond
        zona = self._name()
        op = self._next()
        if op not in (("sym", "=="), ("sym", "!=")):
            raise ConditionSyntaxError(f"Se esperaba '==' o '!=' después de {zona!r}")
        hoja = ZoneIs(zona, self._name())
        return hoja if op[1] == "==" else Not(hoja)

    def _name(self) -> str:
        tok = self._peek()
        if tok and tok[0] == "str":
            self.i += 1
            return tok[1][1:-1].strip()
        palabras: list[str] = []
        while (tok := self._peek()) and tok[0] == "word":
            palabras.append(tok[1])
            self.i += 1
        if not palabras:
            raise ConditionSyntaxError(f"Se esperaba un nombre, se encontró {tok[1] if tok else 'fin'!r}")
        return " ".join(palabras)


def parse_expression(text: str) -> Condition:
    """Parsea texto tipo ``SI (Zona 1 == Baseline) Y NOT (Zona 2 == "Plantilla B")``.

    Todo lo que vaya después de ``==>`` (la parte de acción) se ignora.
    Los nombres que contengan palabras reservadas (Y, O, OR, NOT...) deben ir
    entre comillas.
    """
    condicion = text.split("==>", 1)[0]
    if not condicion.strip():
        raise ConditionSyntaxError("Expresión vacía")
    return _Parser(condicion).parse()


def _quote(name: str) -> str:
    """Pone comillas si el nombre contiene palabras reservadas o símbolos."""
    palabras = name.split()
    if (not palabras or any(p.casefold() in _KEYWORDS for p in palabras)
            or re.search(r"[()\"'=!&|]", name)):
        return '"' + name.replace('"', "'") + '"'
    return name


def condition_to_text(cond: Condition) -> str:
    """Representación legible y re-parseable con ``parse_expression``."""
    if isinstance(cond, ZoneIs):
        objetivo = "Baseline" if cond.target == BASELINE else _quote(cond.target)
        return f"{_quote(cond.zone)} == {objetivo}"
    if isinstance(cond, Not):
        inner = cond.item
        if isinstance(inner, ZoneIs):
            return condition_to_text(inner).replace(" == ", " != ", 1)
        return f"NOT ({condition_to_text(inner)})"
    if isinstance(cond, (And, Or)):
        sep = " Y " if isinstance(cond, And) else " O "
        partes = []
        for c in cond.items:
            t = condition_to_text(c)
            partes.append(f"({t})" if isinstance(c, (And, Or)) else t)
        return sep.join(partes)
    raise TypeError(f"Condición desconocida: {cond!r}")


# --------------------------------------------------------------------------- #
# Reglas, procesos y loops
# --------------------------------------------------------------------------- #
class Trigger(str, Enum):
    EDGE = "flanco"   # sólo en la transición FALSO -> VERDADERO
    LEVEL = "nivel"   # cada ciclo en que la condición sea VERDADERA


def parse_range(value: Any) -> tuple[float, float]:
    """``[x, y]`` | número | None -> (min, max) validado, en segundos."""
    if value is None:
        return 0.0, 0.0
    if isinstance(value, (int, float)):
        a = b = float(value)
    else:
        a, b = (float(v) for v in value)
    if a < 0 or b < a:
        raise ValueError(f"Rango de delay inválido: [{a}, {b}]")
    return a, b


@dataclass
class Rule:
    """``SI condición ==> acciones`` con delay entrópico previo ``[x, y]``."""

    name: str
    condition: Condition
    actions: list[dict[str, Any]] = field(default_factory=list)
    delay: tuple[float, float] = (0.0, 0.0)
    trigger: Trigger = Trigger.EDGE
    enabled: bool = True
    _last: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.delay = parse_range(self.delay)
        self.trigger = Trigger(self.trigger)

    def check(self, obs: Observations, default_threshold: float) -> bool:
        """Evalúa la condición y actualiza el estado de flanco. True = disparar."""
        if not self.enabled:
            return False
        actual = self.condition.evaluate(obs, default_threshold)
        disparar = actual and (self.trigger is Trigger.LEVEL or not self._last)
        self._last = actual
        return disparar

    def reset(self) -> None:
        self._last = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "nombre": self.name,
            "condicion": self.condition.to_dict(),
            "acciones": list(self.actions),
            "delay": list(self.delay),
            "disparo": self.trigger.value,
            "activa": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        return cls(
            name=str(data["nombre"]),
            condition=parse_condition(data["condicion"]),
            actions=[dict(a) for a in data.get("acciones", [])],
            delay=parse_range(data.get("delay")),
            trigger=Trigger(data.get("disparo", Trigger.EDGE.value)),
            enabled=bool(data.get("activa", True)),
        )


@dataclass
class Process:
    """Contenedor de reglas (p. ej. "Proceso 1").

    ``exclusive=True``: en cada ciclo se dispara como mucho la PRIMERA regla
    verdadera (como un SI / SI NO SI). Es lo más seguro porque tras ejecutar
    una acción la pantalla cambia y el resto de reglas se evaluaría sobre una
    captura obsoleta. Con ``False`` se disparan todas las que cumplan.
    """

    name: str
    rules: list[Rule] = field(default_factory=list)
    enabled: bool = True
    exclusive: bool = True

    def evaluate(self, obs: Observations, default_threshold: float) -> list[Rule]:
        if not self.enabled:
            return []
        disparadas: list[Rule] = []
        for regla in self.rules:
            if regla.check(obs, default_threshold):
                disparadas.append(regla)
                if self.exclusive:
                    break
        return disparadas

    def reset(self) -> None:
        for r in self.rules:
            r.reset()

    def to_dict(self) -> dict[str, Any]:
        return {"nombre": self.name, "activo": self.enabled, "exclusivo": self.exclusive,
                "reglas": [r.to_dict() for r in self.rules]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Process:
        return cls(
            name=str(data["nombre"]),
            rules=[Rule.from_dict(r) for r in data.get("reglas", [])],
            enabled=bool(data.get("activo", True)),
            exclusive=bool(data.get("exclusivo", True)),
        )


@dataclass
class Loop:
    """Bucle (p. ej. "Loop 1") que repite sus procesos.

    ``iterations=None`` -> continuo hasta que se detenga el motor.
    ``delay`` -> delay entrópico [x, y] entre iteraciones.
    """

    name: str
    processes: list[Process] = field(default_factory=list)
    iterations: int | None = None
    delay: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        self.delay = parse_range(self.delay)
        if self.iterations is not None and self.iterations < 1:
            raise ValueError(f"{self.name}: iteraciones debe ser >= 1 o None (continuo)")

    @property
    def continuous(self) -> bool:
        return self.iterations is None

    def reset(self) -> None:
        for p in self.processes:
            p.reset()

    def to_dict(self) -> dict[str, Any]:
        return {"nombre": self.name, "iteraciones": self.iterations, "delay": list(self.delay),
                "procesos": [p.to_dict() for p in self.processes]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Loop:
        it = data.get("iteraciones")
        return cls(
            name=str(data["nombre"]),
            processes=[Process.from_dict(p) for p in data.get("procesos", [])],
            iterations=None if it in (None, 0) else int(it),
            delay=parse_range(data.get("delay")),
        )


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """Plantilla en disco + tamaño de la ventana cuando se recortó (para reescalar)."""

    path: str
    ref_size: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"archivo": self.path, "ref": list(self.ref_size) if self.ref_size else None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TemplateRef:
        ref = data.get("ref")
        return cls(path=str(data["archivo"]), ref_size=(int(ref[0]), int(ref[1])) if ref else None)


@dataclass
class Flow:
    """Definición completa ejecutable por el motor (lo que se guarda en .tarea)."""

    name: str
    zones: list[RelativeZone] = field(default_factory=list)
    templates: dict[str, TemplateRef] = field(default_factory=dict)
    loops: list[Loop] = field(default_factory=list)

    def zone(self, name: str) -> RelativeZone | None:
        return next((z for z in self.zones if z.name == name), None)

    def iter_rules(self) -> Iterator[Rule]:
        for loop in self.loops:
            for proc in loop.processes:
                yield from proc.rules

    def requirements(self) -> dict[str, set[str]]:
        """zona -> {objetivos} que el motor debe medir en cada ciclo."""
        reqs: dict[str, set[str]] = {}
        for regla in self.iter_rules():
            if regla.enabled:
                for zona, objetivo in regla.condition.requirements():
                    reqs.setdefault(zona, set()).add(objetivo)
        return reqs

    def validate(self) -> list[str]:
        """Lista de errores (vacía si el flujo es ejecutable)."""
        errores: list[str] = []
        nombres = [z.name for z in self.zones]
        if len(nombres) != len(set(nombres)):
            errores.append("Hay zonas con nombre duplicado")
        if not self.loops:
            errores.append("El flujo no tiene ningún Loop")
        for zona, objetivos in self.requirements().items():
            if zona not in nombres:
                errores.append(f"La zona {zona!r} no existe")
            for obj in objetivos - {BASELINE}:
                if obj not in self.templates:
                    errores.append(f"La plantilla {obj!r} (usada en {zona!r}) no existe")
        return errores

    def reset(self) -> None:
        for loop in self.loops:
            loop.reset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nombre": self.name,
            "zonas": [z.to_dict() for z in self.zones],
            "plantillas": {k: v.to_dict() for k, v in self.templates.items()},
            "loops": [l.to_dict() for l in self.loops],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Flow:
        return cls(
            name=str(data.get("nombre", "Sin nombre")),
            zones=[RelativeZone.from_dict(z) for z in data.get("zonas", [])],
            templates={k: TemplateRef.from_dict(v) for k, v in data.get("plantillas", {}).items()},
            loops=[Loop.from_dict(l) for l in data.get("loops", [])],
        )
