"""The wizard's question flow.

Sections and keys come from ``cdk/stacks/configuration.KEY_DOCS`` in
``KEY_SECTIONS`` order: keys that are not advanced are always asked, advanced
ones only after "Configure advanced settings for <section>?". Every answer is
checked by building the complete mapping (answers so far on top of the
template and ``DEFAULTS``) and running ``validate_mapping`` on it, so the
wizard rejects exactly what ``cdk synth`` would and shows the same message.

Defaults come from, in increasing precedence, ``DEFAULTS``, the shipped
template (``cdk/config/<template>.json``), the ``--output`` file when it
already exists (edit mode), and ``--answers``. Template values that are only
examples (``example.com`` addresses, the ``111122223333`` documentation
account) are never kept silently: the operator must type a real value.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cdk.stacks.configuration import (
    CONFIG_DIR,
    DEFAULTS,
    KEY_DOCS,
    KEY_SECTIONS,
    KeyDoc,
    describe_key,
    validate_mapping,
)
from tools.preflight import CheckResult, Options
from tools.preflight.checks import MANAGED_INVOCATION_LOG_GROUP
from tools.preflight.context import REPO_ROOT

from .console import (
    Console,
    parse_float,
    parse_int,
    parse_list,
    parse_non_negative_float,
    parse_non_negative_int,
    render,
)
from .live import LiveChecks, ModelChoice

SECTION_TITLES: dict[str, str] = {
    "identity": "Identity provider",
    "console": "Admin console",
    "logging": "Bedrock invocation logging",
    "models": "Allowed models",
    "access": "Broker access",
    "limits": "Default limits",
    "enforcement": "Enforcement",
    "retention": "Retention",
    "alerts": "Alerts",
    "workloads": "Workloads",
    "operations": "Operations",
}

# Keys whose answers only make sense together: the validator sees the whole
# group at once (an issuer without an audience, admin_ui without the admin
# claim, unmanaged logging without a log group are all rejected), and a
# rejected group is asked again from its first key.
GROUPS: tuple[tuple[str, ...], ...] = (
    ("jwt_issuer", "jwt_audience"),
    ("admin_ui", "admin_jwt_claim", "admin_jwt_value"),
    ("manage_invocation_logging", "invocation_log_group_name"),
)

# Keys only asked when another answer makes them meaningful; otherwise they
# are reset to their default so the written file stays consistent.
DEPENDENT_KEYS: frozenset[str] = frozenset(
    {
        "jwt_audience",
        "jwt_jwks_url",
        "admin_ui_client_id",
        "admin_ui_connect_origins",
        "admin_email",
        "invocation_log_group_name",
    }
)

# Markers of example values in the shipped templates (RFC 2606 domain and
# the AWS documentation account); such values must be replaced.
PLACEHOLDER_MARKERS: tuple[str, ...] = ("example.com", "111122223333")

WORKLOADS_FILE = "workloads.json"
PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly")
THRESHOLD_ACTIONS: tuple[str, ...] = ("warn", "block")


class ValidationFailed(Exception):
    """The final values are rejected, or a non-interactive answer is."""


class SetupError(Exception):
    """A usage problem (unreadable template or output file)."""


# --- helpers -------------------------------------------------------------------


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def is_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value for marker in PLACEHOLDER_MARKERS)
    if isinstance(value, (list, tuple)):
        return any(is_placeholder(item) for item in value)
    if isinstance(value, Mapping):
        return any(is_placeholder(item) for item in value.values())
    return False


def load_json_object(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SetupError(f"cannot read {what} {path}: {exc.strerror or exc}") from None
    except json.JSONDecodeError as exc:
        raise SetupError(f"{what} {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SetupError(f"{what} {path} must contain a JSON object")
    return data


def resolve_file(text: str, search_dirs: Sequence[Path]) -> Path:
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    for directory in search_dirs:
        candidate = directory / path
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(text)


def anchor_file_paths(
    values: dict[str, Any], output_dir: Path, search_dirs: Sequence[Path]
) -> None:
    """Relative ``model_config``/``workloads`` paths resolve from the output
    file's directory. When the file is not there but lives in one of
    ``search_dirs`` (the shipped ``cdk/config/``), store its absolute path so
    validation here and ``cdk synth`` later find the same file."""
    for key in ("model_config", "workloads"):
        raw = values.get(key)
        if not isinstance(raw, str) or not raw.strip() or raw.strip().startswith("{"):
            continue
        path = Path(raw.strip()).expanduser()
        if path.is_absolute() or (output_dir / path).is_file():
            continue
        for directory in search_dirs:
            candidate = directory / path
            if candidate.is_file():
                values[key] = str(candidate.resolve())
                break


def grouped(docs: Sequence[KeyDoc]) -> list[list[KeyDoc]]:
    """Split ``docs`` (one section, in order) into validation groups."""
    by_name = {doc.name: doc for doc in docs}
    consumed: set[str] = set()
    result: list[list[KeyDoc]] = []
    for doc in docs:
        if doc.name in consumed:
            continue
        members = next((group for group in GROUPS if doc.name in group), (doc.name,))
        group = [by_name[name] for name in members if name in by_name]
        consumed.update(member.name for member in group)
        result.append(group)
    return result


def parse_thresholds(text: str) -> list[dict[str, Any]] | None:
    """``50:warn,80:warn,100:block`` -> ``[{"at": 0.5, "action": "warn"}, ...]``;
    empty means "use the defaults derived from warn_threshold"."""
    entries = parse_list(text)
    if not entries:
        return None
    result: list[dict[str, Any]] = []
    for entry in entries:
        percent, separator, action = entry.partition(":")
        action = action.strip()
        if not separator or action not in THRESHOLD_ACTIONS:
            raise ValueError(
                f"Expected percent:warn or percent:block entries, got {entry!r}."
            )
        try:
            value = float(percent)
        except ValueError:
            raise ValueError(f"Expected a percentage before ':' in {entry!r}.") from None
        if value <= 0:
            raise ValueError("Threshold percentages must be greater than 0.")
        result.append({"at": value / 100, "action": action})
    return result


def render_thresholds(thresholds: Any) -> str:
    if not isinstance(thresholds, list):
        return ""
    parts = []
    for entry in thresholds:
        if isinstance(entry, dict) and "at" in entry and "action" in entry:
            try:
                parts.append(f"{float(entry['at']) * 100:g}:{entry['action']}")
            except (TypeError, ValueError):
                continue
    return ",".join(parts)


def describe_limits(limits: Any) -> str:
    if not isinstance(limits, Mapping):
        return json.dumps(limits)
    parts = []
    for period in PERIODS:
        entry = limits.get(period)
        if not isinstance(entry, Mapping):
            parts.append(f"{period} off")
            continue
        try:
            usd = float(entry.get("usd", 0))
            usd_text = f"{usd:,.2f} USD" if usd else "no USD cap"
            tokens = []
            for dimension in ("input_tokens", "output_tokens"):
                count = int(entry.get(dimension, 0))
                tokens.append(f"{count:,}" if count else "unlimited")
            text = f"{period} {usd_text}, {tokens[0]} input / {tokens[1]} output tokens"
        except (TypeError, ValueError):
            text = f"{period} {json.dumps(entry, sort_keys=True)}"
        if entry.get("thresholds"):
            text += f", {len(entry['thresholds'])} thresholds"
        parts.append(text)
    return "; ".join(parts)


def describe_workload(entry: Mapping[str, Any]) -> str:
    text = str(entry.get("model", "?"))
    if entry.get("role_arn"):
        text += f", role {entry['role_arn']}"
    return text


def flagged_models(result: CheckResult) -> list[str]:
    """Models the ``bedrock_model_access`` check reported as not available."""
    prefix = "not available: "
    for line in result.detail.splitlines():
        if line.startswith(prefix):
            entries = line[len(prefix):].split("; ")
            return [entry.split(":", 1)[0].strip() for entry in entries if entry.strip()]
    return []


# --- the wizard ------------------------------------------------------------------


class Wizard:
    """Asks the questions and keeps a complete, validated working mapping.

    ``values`` always holds every deployment key (defaults, template, the
    edited file, then the answers); ``answered`` records what was asked and
    answered, for ``--save-answers``.
    """

    def __init__(
        self,
        console: Console,
        *,
        template: str,
        output: Path | str,
        live: LiveChecks | None = None,
        answers: Mapping[str, Any] | None = None,
        root: Path = REPO_ROOT,
    ) -> None:
        self.console = console
        self.template = template
        self.output = Path(output)
        self.output_dir = self.output.resolve().parent
        self.live = live
        self.answers: dict[str, Any] = dict(answers or {})
        self.root = Path(root)
        self.account = live.account if live is not None else None
        self.template_values = load_json_object(
            CONFIG_DIR / f"{template}.json", "template"
        )
        self.existing = (
            load_json_object(self.output, "output file") if self.output.is_file() else {}
        )
        # What "(changed from template)" compares against.
        self.reference: dict[str, Any] = copy.deepcopy(
            {**self.template_values, **self.existing}
        )
        values = {
            key: copy.deepcopy(value) for key, value in DEFAULTS.items() if value is not None
        }
        values.update(copy.deepcopy(self.reference))
        anchor_file_paths(values, self.output_dir, (self.output_dir, CONFIG_DIR))
        self.values: dict[str, Any] = values
        self.placeholders: dict[str, Any] = {
            key: copy.deepcopy(value)
            for key, value in self.reference.items()
            if is_placeholder(value)
        }
        self.answered: dict[str, Any] = {}
        self.acknowledged_logging_overwrite = False
        self.flagged_models: list[str] = []
        self.baseline_errors: list[str] = []
        self.sections: list[str] = list(KEY_SECTIONS) + sorted(
            {doc.section for doc in KEY_DOCS} - set(KEY_SECTIONS)
        )
        self._menu_shown = False

    # --- state ---------------------------------------------------------------

    def validate(self, values: Mapping[str, Any] | None = None) -> list[str]:
        return validate_mapping(
            self.values if values is None else values,
            base_dir=self.output_dir,
            account=self.account,
        )

    def applicable(self, name: str) -> bool:
        issuer = bool(str(self.values.get("jwt_issuer") or "").strip())
        admin_ui = truthy(self.values.get("admin_ui"))
        if name in ("jwt_audience", "jwt_jwks_url"):
            return issuer
        if name in ("admin_ui_client_id", "admin_ui_connect_origins"):
            return admin_ui and issuer
        if name == "admin_email":
            return admin_ui and not issuer
        if name == "invocation_log_group_name":
            return not truthy(self.values.get("manage_invocation_logging"))
        return True

    def reset(self, name: str) -> None:
        if DEFAULTS.get(name) is not None:
            self.values[name] = copy.deepcopy(DEFAULTS[name])
        else:
            self.values.pop(name, None)

    def reset_inapplicable(self, up_to_section: int | None = None) -> None:
        for doc in KEY_DOCS:
            if doc.name not in DEPENDENT_KEYS:
                continue
            if up_to_section is not None and self.sections.index(doc.section) > up_to_section:
                continue  # not reached yet: keep its template value for later
            if not self.applicable(doc.name):
                self.reset(doc.name)

    def suggest(self, name: str, value: Any) -> None:
        """A live finding proposes a default; explicit --answers win."""
        if name not in self.answers:
            self.values[name] = value

    def written_values(self) -> dict[str, Any]:
        """What goes into the file: every key the template (or the edited
        file) sets, plus every value that differs from ``DEFAULTS``."""
        return {
            key: copy.deepcopy(value)
            for key, value in self.values.items()
            if key in self.reference or value != DEFAULTS.get(key)
        }

    def changed_keys(self, written: Mapping[str, Any]) -> set[str]:
        return {
            key
            for key, value in written.items()
            if value != self.reference.get(key, DEFAULTS.get(key))
        }

    # --- flow ----------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Ask everything; return the complete, validated mapping."""
        self.intro()
        if self.live is not None:
            self.console.say()
            self.live.run("region_support", self.values)
        self.baseline_errors = self.validate()
        if self.baseline_errors:
            self.console.say(
                f"\nNote: the current values are rejected ({self.baseline_errors[0]}); "
                "fix this when the key comes up."
            )
        for index, section in enumerate(self.sections):
            self.run_section(index, section)
        self.reset_inapplicable()
        errors = self.validate()
        if errors:
            raise ValidationFailed(errors[0])
        remaining = sorted(
            key for key, value in self.placeholders.items() if self.values.get(key) == value
        )
        if remaining:
            raise ValidationFailed(
                "these keys still hold the template's example values: " + ", ".join(remaining)
            )
        return self.values

    def intro(self) -> None:
        say = self.console.say
        say("Bedrock Spend Controls setup")
        mode = "editing" if self.existing else "writing"
        say(f"Template: {self.template} (cdk/config/{self.template}.json); {mode} {self.output}")
        say("Enter keeps the value in brackets, '-' clears a value, Ctrl-C aborts without writing.")
        if self.live is not None:
            target = f"account {self.live.account or 'unknown'}, region {self.live.region}"
            if self.live.profile:
                target += f", profile {self.live.profile}"
            say(f"Live checks: on ({target}).")
        else:
            say("Live checks: off.")

    def run_section(self, index: int, section: str) -> None:
        docs = [doc for doc in KEY_DOCS if doc.section == section]
        basic = [doc for doc in docs if not doc.advanced]
        advanced = [doc for doc in docs if doc.advanced]
        title = SECTION_TITLES.get(section, section.replace("_", " ").capitalize())
        if self.console.interactive:
            self.console.say()
            self.console.say(f"== {title} ({index + 1}/{len(self.sections)}) ==")
        if section == "workloads":
            self.workloads_section()
            basic = [doc for doc in basic if doc.name != "workloads"]
        for group in grouped(basic):
            self.ask_group(group, index)
        advanced = [doc for doc in advanced if self.applicable(doc.name)]
        if advanced:
            default = any(doc.name in self.answers for doc in advanced)
            if self.console.ask_bool(f"Configure advanced settings for {section}?", default):
                for group in grouped(advanced):
                    self.ask_group(group, index)

    def ask_group(self, docs: Sequence[KeyDoc], section_index: int) -> None:
        names = [doc.name for doc in docs]
        self.before_group(names)
        # The values the group started with: a rejected answer must not
        # become the suggestion shown on the retry, while the other keys of
        # the group keep what the operator just typed.
        previous = {name: copy.deepcopy(self.values.get(name)) for name in names}
        while True:
            for doc in docs:
                if self.applicable(doc.name):
                    self.ask_doc(doc)
                else:
                    self.reset(doc.name)
            self.reset_inapplicable(section_index)
            errors = self.validate()
            if errors and errors != self.baseline_errors:
                self.console.say(f"  Error: {errors[0]}")
                if not self.console.interactive:
                    raise ValidationFailed(errors[0])
                # Validator messages lead with the offending key; the other
                # keys they mention are context, not culprits.
                mentioned = [(errors[0].find(name), name) for name in names if name in errors[0]]
                blamed = [min(mentioned)[1]] if mentioned else names
                for name in names:
                    self.answers.pop(name, None)
                    if name not in blamed:
                        continue
                    self.answered.pop(name, None)
                    if previous[name] is None:
                        self.values.pop(name, None)
                    else:
                        self.values[name] = copy.deepcopy(previous[name])
                continue
            self.baseline_errors = errors
            if self.console.interactive and self.live is not None and not self.after_group(names):
                continue
            return

    def ask_doc(self, doc: KeyDoc) -> None:
        value = self.prompt_value(doc)
        self.values[doc.name] = value
        self.answered[doc.name] = copy.deepcopy(value)

    def prompt_value(self, doc: KeyDoc) -> Any:
        name = doc.name
        suggested = self.answers.get(name, self.values.get(name))
        example: str | None = None
        required = False
        if name in self.placeholders and suggested == self.placeholders[name]:
            example = render(suggested)
            if isinstance(suggested, list) and len(suggested) > 1:
                example = f"{render(suggested[:1])}, ..."
            suggested, required = None, True
        if self.console.interactive:
            self.console.say(f"  {doc.help}.")
        ask = self.console
        shown = render(suggested)
        options = {"example": example, "required": required}
        if doc.type == "bool":
            return ask.ask_bool(name, None if suggested is None else truthy(suggested))
        if doc.type == "int":
            return ask.ask_value(name, shown, parse_int, **options)
        if doc.type == "float":
            return ask.ask_value(name, shown, parse_float, **options)
        if doc.type == "string_list":
            if name == "allowed_model_arns":
                return self.prompt_models(name, suggested, example, required)
            return ask.ask_value(name, shown, parse_list, **options)
        if doc.type == "object":
            if name == "default_limits":
                return self.prompt_default_limits(suggested)
            default = None if suggested is None else json.dumps(suggested, sort_keys=True)
            return ask.ask_value(name, default, parse_json_object, **options)
        return ask.ask_text(name, None if suggested is None else str(suggested), **options)

    # --- live hooks ----------------------------------------------------------

    def before_group(self, names: Sequence[str]) -> None:
        if self.live is None or not self.console.interactive:
            return
        if "manage_invocation_logging" in names:
            self.suggest_logging()
        if "reserve_enforcement_concurrency" in names:
            self.suggest_concurrency()

    def suggest_logging(self) -> None:
        assert self.live is not None
        existing = self.live.existing_logging()
        region = self.live.region
        say = self.console.say
        if existing is None:
            say(f"  No Bedrock invocation logging is configured in {region}; the stack can own it.")
            self.suggest("manage_invocation_logging", True)
            self.suggest("invocation_log_group_name", "")
        elif existing.log_group == MANAGED_INVOCATION_LOG_GROUP:
            say(
                f"  Invocation logging in {region} already delivers to {existing.log_group} "
                "(managed by this stack)."
            )
            self.suggest("manage_invocation_logging", True)
            self.suggest("invocation_log_group_name", "")
        else:
            say(f"  Invocation logging in {region} currently delivers to {existing.description}.")
            if existing.log_group:
                say(
                    "  Suggested: manage_invocation_logging=false with that log group, which keeps "
                    "the existing setup."
                )
                self.suggest("manage_invocation_logging", False)
                self.suggest("invocation_log_group_name", existing.log_group)
            else:
                say(
                    "  manage_invocation_logging=true would overwrite it; false needs a CloudWatch "
                    "log group that already receives invocation logs."
                )

    def suggest_concurrency(self) -> None:
        assert self.live is not None
        probe = {**self.values, "reserve_enforcement_concurrency": True}
        result = self.live.run("lambda_concurrency", probe)
        if result.status == "fail":
            self.console.say("  Suggested: reserve_enforcement_concurrency=false in this account.")
            self.suggest("reserve_enforcement_concurrency", False)

    def after_group(self, names: Sequence[str]) -> bool:
        """Live checks for the answers just given; False asks the group again."""
        assert self.live is not None
        ask = self.console.ask_bool
        values = self.values
        if "jwt_issuer" in names and str(values.get("jwt_issuer") or "").strip():
            result = self.live.run("oidc_issuer", values)
            if result.status == "fail":
                return not ask("Change the identity provider settings?", True)
        if "manage_invocation_logging" in names:
            if not self.confirm_logging_overwrite():
                return False
            options = Options(acknowledge_logging_overwrite=self.acknowledged_logging_overwrite)
            result = self.live.run("invocation_logging", values, options=options)
            if result.status == "fail":
                return not ask("Change the invocation logging settings?", True)
        if "allowed_model_arns" in names:
            result = self.live.run("bedrock_model_access", values)
            self.flagged_models = flagged_models(result)
            if result.status == "fail":
                return ask("Keep these models anyway (access can be enabled later)?", False)
        if "invoker_principal_arns" in names and values.get("invoker_principal_arns"):
            result = self.live.run("invoker_principals", values)
            if result.status == "fail":
                return ask("Keep these principals anyway?", False)
        return True

    def confirm_logging_overwrite(self) -> bool:
        assert self.live is not None
        if not truthy(self.values.get("manage_invocation_logging")):
            self.acknowledged_logging_overwrite = False
            return True
        existing = self.live.existing_logging()
        if existing is None or existing.log_group == MANAGED_INVOCATION_LOG_GROUP:
            return True
        self.console.say(
            "  Deploying with manage_invocation_logging=true OVERWRITES the Region-wide "
            f"configuration ({existing.description}); it is not restored on destroy."
        )
        self.acknowledged_logging_overwrite = self.console.ask_bool(
            "Accept overwriting the existing invocation logging configuration?", False
        )
        return self.acknowledged_logging_overwrite

    # --- models --------------------------------------------------------------

    def show_model_menu(self, choices: Sequence[ModelChoice]) -> None:
        assert self.live is not None
        if self._menu_shown:
            self.console.say(f"  Model menu: the numbers listed above (1-{len(choices)}).")
            return
        self._menu_shown = True
        self.console.say(
            f"  Models visible in {self.live.region} (foundation models, then cross-Region "
            "inference profiles):"
        )
        width = max(len(choice.identifier) for choice in choices)
        for number, choice in enumerate(choices, 1):
            self.console.say(
                f"  {number:>3}) {choice.identifier.ljust(width)}  {choice.description}"
            )

    def menu_choices(self) -> list[ModelChoice]:
        if self.live is None or not self.console.interactive:
            return []
        return self.live.model_choices()

    def prompt_models(
        self, name: str, suggested: Any, example: str | None, required: bool
    ) -> list[str]:
        choices = self.menu_choices()
        if not choices:
            return self.console.ask_value(
                name, render(suggested), parse_list, example=example, required=required
            )
        self.show_model_menu(choices)
        self.console.say(
            "  Enter menu numbers and/or Bedrock ARNs separated by commas; '*' allows every model."
        )
        live = self.live
        assert live is not None

        def parse(text: str) -> list[str]:
            tokens = parse_list(text)
            if not tokens:
                raise ValueError("Choose at least one model, or '*'.")
            arns: list[str] = []
            for token in tokens:
                if token.isdigit():
                    index = int(token)
                    if not 1 <= index <= len(choices):
                        raise ValueError(f"{token} is not a menu number.")
                    arns.extend(live.arns_for(choices[index - 1]))
                else:
                    arns.append(token)
            return list(dict.fromkeys(arns))

        return self.console.ask_value(
            name, render(suggested), parse, example=example, required=required
        )

    def prompt_model_id(self, label: str) -> str:
        choices = self.menu_choices()
        if not choices:
            return self.console.ask_text(label, None, required=True)
        self.show_model_menu(choices)

        def parse(text: str) -> str:
            token = text.strip()
            if token.isdigit():
                index = int(token)
                if not 1 <= index <= len(choices):
                    raise ValueError(f"{token} is not a menu number.")
                return choices[index - 1].identifier
            return token

        return self.console.ask_value(label, None, parse, required=True)

    # --- default_limits ------------------------------------------------------

    def prompt_default_limits(self, suggested: Any) -> dict[str, Any]:
        if isinstance(suggested, Mapping):
            current = copy.deepcopy(dict(suggested))
        else:
            current = copy.deepcopy(self.values.get("default_limits") or DEFAULTS["default_limits"])
        if not self.console.interactive:
            return current
        ask = self.console
        ask.say(f"  Current: {describe_limits(current)}.")
        if not ask.ask_bool("Change the default limits?", False):
            return current
        ask.say(
            "  0 means unlimited for that dimension. Thresholds are percent:action pairs "
            "(e.g. 50:warn,80:warn,100:block); empty uses the list derived from warn_threshold."
        )
        result: dict[str, Any] = {}
        for period in PERIODS:
            existing = current.get(period)
            if period != "daily" and not ask.ask_bool(
                f"Enable {period} limits?", isinstance(existing, Mapping)
            ):
                result[period] = None
                continue
            base = dict(existing) if isinstance(existing, Mapping) else {}
            entry: dict[str, Any] = {
                "usd": ask.ask_value(
                    f"{period} usd", render(base.get("usd", 0)), parse_non_negative_float
                ),
                "input_tokens": ask.ask_value(
                    f"{period} input_tokens",
                    render(base.get("input_tokens", 0)),
                    parse_non_negative_int,
                ),
                "output_tokens": ask.ask_value(
                    f"{period} output_tokens",
                    render(base.get("output_tokens", 0)),
                    parse_non_negative_int,
                ),
            }
            while True:
                thresholds = ask.ask_value(
                    f"{period} thresholds",
                    render_thresholds(base.get("thresholds")),
                    parse_thresholds,
                )
                if thresholds:
                    entry["thresholds"] = thresholds
                else:
                    entry.pop("thresholds", None)
                probe = {**self.values, "default_limits": {**current, **result, period: entry}}
                errors = self.validate(probe)
                if errors and "thresholds" in errors[0]:
                    ask.say(f"  Error: {errors[0]}")
                    continue
                break
            result[period] = entry
        if current.get("rate") is not None:
            result["rate"] = current["rate"]
        return result

    # --- workloads -----------------------------------------------------------

    def load_roster(self) -> list[dict[str, Any]]:
        """Current workload entries from --answers, the edited file, or the
        template (inline object or file path)."""
        raw = self.answers.get("workloads", self.values.get("workloads"))
        entries: Any = None
        if isinstance(raw, Mapping):
            entries = raw.get("workloads")
        elif isinstance(raw, str) and raw.strip():
            text = raw.strip()
            try:
                if text.startswith("{"):
                    data = json.loads(text)
                else:
                    path = resolve_file(text, (self.output_dir, CONFIG_DIR))
                    data = json.loads(path.read_text(encoding="utf-8"))
                entries = data.get("workloads") if isinstance(data, dict) else None
            except (OSError, ValueError) as exc:
                self.console.say(
                    f"  The current workloads value could not be read ({exc}); starting empty."
                )
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, Mapping)]

    def workloads_section(self) -> None:
        if self.console.interactive:
            self.console.say(f"  {describe_key('workloads').help}.")
        existing = self.load_roster()
        while True:
            entries = self.prompt_roster(existing)
            self.values["workloads"] = {"workloads": entries} if entries else ""
            self.answered["workloads"] = copy.deepcopy(self.values["workloads"])
            errors = self.validate()
            if errors and errors != self.baseline_errors:
                self.console.say(f"  Error: {errors[0]}")
                if not self.console.interactive:
                    raise ValidationFailed(errors[0])
                existing = entries
                continue
            self.baseline_errors = errors
            return

    def prompt_roster(self, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.console.interactive:
            return existing
        ask = self.console
        if not ask.ask_bool("Configure workloads?", bool(existing)):
            return []
        entries = [
            entry
            for entry in existing
            if ask.ask_bool(
                f"Keep workload '{entry.get('name')}' ({describe_workload(entry)})?", True
            )
        ]
        while ask.ask_bool("Add a workload?", not entries):
            entry = self.prompt_workload(entries)
            if entry is not None:
                entries.append(entry)
        return entries

    def prompt_workload(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        ask = self.console
        ask.say(
            "  The name (lowercase letters, digits, hyphens) becomes the quota subject "
            "workload:<name>."
        )
        name = ask.ask_text("workload name", None, required=True)
        model = self.prompt_model_id("workload model (foundation-model or inference-profile ID)")
        role_arn = ask.ask_text(
            "workload role_arn (the application's IAM role; empty = metered and alerted only)", ""
        )
        entry: dict[str, Any] = {"name": name, "model": model}
        if role_arn:
            entry["role_arn"] = role_arn
        probe = {**self.values, "workloads": {"workloads": [*entries, entry]}}
        errors = self.validate(probe)
        if errors:
            ask.say(f"  Error: {errors[0]}")
            return None
        return entry


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Expected a JSON object: {exc}.") from None
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data
