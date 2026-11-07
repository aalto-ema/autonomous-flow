"""Presentation descriptions of protocol steps; never issues device commands."""

import threading
from dataclasses import dataclass, replace

# Each action has a distinct SVG in the same visual family.
ACTIONS = {
    "select": (
        "Select route",
        "Move the selector valve to the required source or destination.",
    ),
    "prime": ("Prime source line", "Fill a source tube with its assigned liquid."),
    "condition": (
        "Condition route",
        "Replace the transition liquid in the route or common line.",
    ),
    "aspirate": ("Aspirate liquid", "Draw liquid into the syringe or common line."),
    "dispense": (
        "Dispense liquid",
        "Push liquid from the syringe toward the selected destination.",
    ),
    "transfer": (
        "Deliver ingredient",
        "Transfer a stock solution or water into the mixing vessel.",
    ),
    "prepare": (
        "Prepare mixture",
        "Combine the recipe ingredients in the mixing vessel.",
    ),
    "chase_water": (
        "Water chase",
        "Use water to advance a liquid plug through the tubing.",
    ),
    "chase_air": (
        "Air chase / purge",
        "Use air to advance or clear liquid from a route.",
    ),
    "load_cell": (
        "Load flow cell",
        "Advance the mixture to or beyond the cell boundary.",
    ),
    "displace": (
        "Replace cell sample",
        "Advance fresh mixture between stopped-flow measurements.",
    ),
    "flow": ("Continuous flow", "Pump mixture through the cell while measuring."),
    "measure": ("Measure", "Acquire CA, CV or LSV data with the EmStat."),
    "drain": (
        "Drain to waste",
        "Remove residual or transition liquid to the waste route.",
    ),
    "rinse_vessel": (
        "Rinse mixing vessel",
        "Wash the mixing vessel with water and remove the rinse.",
    ),
    "rinse_cell": ("Rinse flow cell", "Flush the flow-cell route with water."),
    "rinse_line": ("Rinse source lines", "Wash recovered source tubing with water."),
    "recover": (
        "Recover source liquid",
        "Return recoverable source liquid through its assigned route.",
    ),
    "air_empty": (
        "Empty with air",
        "Remove liquid remaining in maintained source lines.",
    ),
    "flush": (
        "Flush system",
        "Clear the configured routes using the selected flushing procedure.",
    ),
    "reset": ("Initialize / home", "Initialize the syringe or home the selector."),
    "wait": (
        "Wait / equilibrate",
        "Wait for the instrument or the configured measurement equilibration.",
    ),
    "pause": ("Pause", "Finish the current stage and preserve progress."),
    "stop": (
        "Stop requested",
        "Request stopping; verify physical state before continuing.",
    ),
    "complete": (
        "Complete",
        "The software reports that the step or protocol has finished.",
    ),
    "failed": (
        "Recovery required",
        "A step failed or was interrupted; inspect actual liquid and device state.",
    ),
}


@dataclass(frozen=True)
class ProtocolStep:
    number: int
    action: str
    title: str
    liquid: str
    route: str
    detail: str
    status: str = "planned"
    stage: str = ""


def classify(text):
    t = text.lower().replace("_", " ")
    rules = [
        ("stop", ("emergency", "abort", "stop requested")),
        ("pause", ("pause",)),
        ("reset", ("reset", "home", "initialize")),
        ("select", ("select ", "move selector")),
        ("air_empty", ("air-empty", "air empty", "empty maintained")),
        ("rinse_line", ("rinse recovered", "rinse source", "rinsing source")),
        ("rinse_vessel", ("rinse mixing", "rinsing mixing")),
        ("rinse_cell", ("rinse flow", "rinsing flow")),
        ("recover", ("recover",)),
        ("chase_water", ("chase water",)),
        ("chase_air", ("chase air", "purge air", "aspirate air", "draw air")),
        ("drain", ("discard", "drain", "waste", "empty syringe", "emptying syringe")),
        ("condition", ("condition", "transition")),
        ("displace", ("displacement", "between measurement")),
        (
            "load_cell",
            (
                "approach",
                "advance mixture",
                "advance through",
                "load flow",
                "load stopped",
            ),
        ),
        (
            "flow",
            ("continuously", "continuous flow", "continuous dispense", "plug through"),
        ),
        ("prime", ("prime", "priming")),
        ("aspirate", ("aspirate", "aspirat", "draw ")),
        ("dispense", ("dispense", "dispens", "push ")),
        ("transfer", ("transfer", "deliver")),
        ("prepare", ("prepare", "preparing")),
        ("rinse_line", ("rinse", "rinsing")),
        ("measure", ("measure", "measuring", "chronoamperometry", "voltammetry")),
        ("flush", ("flush",)),
    ]
    return next(
        (key for key, words in rules if any(word in t for word in words)), "wait"
    )


class ProtocolTracker:
    def __init__(self):
        self.lock = threading.RLock()
        self.steps = []
        self.counter = 0
        self.selected = "Unconfirmed route"
        self.stage = ""
        self.routes = {}
        self.contents = "Unconfirmed liquid"
        self.origin = "Unconfirmed route"

    def observe(self, name, target, status):
        with self.lock:
            if status == "started":
                self.counter += 1
                action = classify(name)
                text = name.lower().split(" (mixture transfer")[0]
                liquid = (
                    "Air"
                    if action in {"chase_air", "air_empty"}
                    else "Water"
                    if "water" in text or "rinse" in text or action.startswith("rinse")
                    else "Mixture"
                    if "mixture" in text
                    or action in {"measure", "flow", "load_cell", "displace"}
                    else self.selected
                    if action not in {"select", "reset", "wait", "stop", "pause"}
                    else "—"
                )
                # Selector destinations are not liquid identities. Retain the
                # source of an aspirated liquid across later valve moves.
                if action in {"select", "reset", "wait", "pause", "stop"}:
                    liquid = "—"
                selected_liquid = self.routes.get(self.selected, self.selected)
                if action in {"aspirate", "condition", "prime"}:
                    if liquid == self.selected:
                        liquid = selected_liquid
                    self.contents = liquid
                    self.origin = self.selected
                if (
                    action in {"dispense", "drain", "transfer"}
                    and "water" not in text
                    and "mixture" not in text
                ):
                    liquid = self.contents
                route = (
                    self.selected
                    if action
                    not in {
                        "dispense",
                        "drain",
                        "transfer",
                        "load_cell",
                        "flow",
                        "displace",
                    }
                    else f"{self.origin} → {self.selected}"
                )
                if "outlet" in text and action in {"dispense", "drain", "chase_air"}:
                    route = f"{self.origin} → syringe outlet / waste"
                self.steps.append(
                    ProtocolStep(
                        self.counter,
                        action,
                        ACTIONS[action][0],
                        liquid,
                        route,
                        name,
                        "running",
                        self.stage,
                    )
                )
                self.steps = self.steps[-500:]
            else:
                for index in range(len(self.steps) - 1, -1, -1):
                    step = self.steps[index]
                    if step.detail == name and step.status == "running":
                        self.steps[index] = replace(step, status=status)
                        break
                if status == "completed" and name.lower().startswith("select "):
                    label = name[7:]
                    self.selected = next(
                        (
                            route
                            for route in sorted(self.routes, key=len, reverse=True)
                            if label.lower().startswith(route.lower())
                        ),
                        label,
                    )

    def snapshot(self):
        with self.lock:
            return tuple(self.steps)


def preview_protocol(plan):
    steps = []

    def add(stage, action, liquid, route, detail):
        steps.append(
            ProtocolStep(
                len(steps) + 1,
                action,
                ACTIONS[action][0],
                liquid,
                route,
                detail,
                stage=stage,
            )
        )

    for s in plan.sources:
        if s.fraction > 0:
            add(
                "Prime",
                "prime",
                s.source_config_key,
                f"Port {s.selector_port} → source line",
                "Only if the source is not already primed; inspect its initial contents.",
            )
    for t in plan.mixture_transfers:
        add(
            "Prepare",
            "transfer",
            t.source_config_key,
            f"Port {t.selector_port} → mixing vessel",
            f"{t.nominal_volume_ml:.6f} mL nominal • {t.syringe_steps} steps • {t.liquid_stroke_count} stroke(s). Route selection, aspiration, delivery and route clearing occur within each transfer.",
        )
    add(
        "Measure",
        "condition",
        "Mixture",
        "Mixing vessel → common line",
        "Condition the mixing route; discard transition liquid to waste.",
    )
    add(
        "Measure",
        "load_cell",
        "Mixture",
        "Common line → flow cell",
        "Load and approach the flow-cell boundary using the reviewed routing volumes.",
    )
    if plan.workflow_kind == "continuous_flow":
        add(
            "Measure",
            "flow",
            "Mixture",
            "Mixing vessel → flow cell",
            "Flow and electrochemical acquisition overlap.",
        )
    else:
        add(
            "Measure",
            "chase_water",
            "Water",
            "Syringe → common line → flow cell",
            "Water advances the stopped-flow mixture plug.",
        )
    for i in range(plan.measurement_count):
        if i:
            add(
                "Measure",
                "displace",
                "Mixture",
                "Common line → flow cell",
                f"Replace sample before measurement {i + 1}: {plan.between_measurement_displacement_volume_ml:g} mL nominal.",
            )
        if plan.measurement_settings.equilibration_time_s:
            add(
                "Measure",
                "wait",
                "Mixture",
                "Flow cell",
                f"Configured equilibration: {plan.measurement_settings.equilibration_time_s:g} s.",
            )
        add(
            "Measure",
            "measure",
            "Mixture",
            "EmStat electrodes in flow cell",
            f"{plan.measurement_type.replace('_', ' ')} • measurement {i + 1}/{plan.measurement_count}",
        )
    if plan.workflow_kind == "stopped_flow":
        add(
            "Measure",
            "rinse_cell",
            "Water",
            "Syringe → flow cell → waste",
            "Final stopped-flow chase empties the retained rinse water after the measurements.",
        )
    add(
        "Clean",
        "drain",
        "Residual mixture",
        "Mixing vessel → waste",
        "Drain remaining mixture using the tracked vessel volume.",
    )
    add(
        "Clean",
        "rinse_vessel",
        "Water",
        "Water source → mixing vessel → waste",
        f"{plan.cleanup_settings.mixing_rinse_cycles} rinse cycle(s), {plan.cleanup_settings.mixing_rinse_volume_ml:g} mL each.",
    )
    if plan.workflow_kind == "continuous_flow":
        add(
            "Clean",
            "rinse_cell",
            "Water",
            "Water source → flow cell → waste",
            f"{plan.cleanup_settings.flow_cell_rinse_through_volume_ml:g} mL rinse-through.",
        )
    add(
        "Clean",
        "complete",
        "—",
        "—",
        "End of experiment. Source recovery, rinsing and air emptying are separate maintenance procedures.",
    )
    return tuple(steps)
