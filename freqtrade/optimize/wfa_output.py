from __future__ import annotations

import os
import sys
import time
from typing import Any

import numpy as np
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn
from rich.table import Table
from rich.text import Text

from freqtrade.optimize.walk_forward import WalkForwardWindow, WindowResult


PHASE_LABELS = {
    "hyperopt": "Hyperopt (train)",
    "backtest_optimized": "Backtest (optimized params)",
    "backtest_baseline": "Backtest (baseline params)",
    "holdout_consensus": "Holdout (consensus params)",
    "holdout_baseline": "Holdout (baseline params)",
    "done": "Done",
}


class WFADashboard:
    def __init__(
        self,
        windows: list[WalkForwardWindow],
        strategy: str,
        epochs_per_window: int,
        stake_currency: str,
    ) -> None:
        self._windows = windows
        self._n_windows = len(windows)
        self._strategy = strategy
        self._epochs = epochs_per_window
        self._stake = stake_currency

        self._current_idx = 0
        self._current_window: WalkForwardWindow | None = None
        self._phase = "hyperopt"

        self._ho_epoch = 0
        self._ho_total = epochs_per_window
        self._ho_best: dict[str, Any] | None = None

        self._completed: list[WindowResult] = []
        self._phase_log: dict[int, dict[str, str]] = {}
        for i in range(self._n_windows):
            self._phase_log[i] = {
                "hyperopt": "pending",
                "backtest_optimized": "pending",
                "backtest_baseline": "pending",
            }

        self._start_time = time.monotonic()
        self._live: Live | None = None
        self._saved_fds: dict[int, int] = {}
        self._devnull_fd: int = -1

    def __enter__(self) -> WFADashboard:
        # Redirect stdout/stderr to /dev/null at the OS fd level.
        # Only the Live display keeps a handle to the real terminal.
        # This catches ALL output: loggers, print(), C extensions,
        # libraries creating handlers after this point (tvDatafeed, etc.)
        try:
            stderr_fd = sys.stderr.fileno()
            stdout_fd = sys.stdout.fileno()
        except (AttributeError, OSError):
            self._live = Live(
                console=Console(stderr=True),
                screen=True,
                refresh_per_second=1,
                get_renderable=self._build,
            )
            self._live.__enter__()
            return self

        # Redirect stdout/stderr to /dev/null at the OS fd level.
        # Only Live keeps a handle to the real terminal via os.dup().
        real_stderr_dup = os.dup(stderr_fd)
        self._saved_fds[stderr_fd] = real_stderr_dup

        real_stdout_dup = os.dup(stdout_fd)
        self._saved_fds[stdout_fd] = real_stdout_dup

        self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self._devnull_fd, stderr_fd)
        os.dup2(self._devnull_fd, stdout_fd)

        real_stderr_file = os.fdopen(real_stderr_dup, "w", closefd=False)
        # screen=True: uses alternate terminal buffer (like htop/vim).
        # Each refresh redraws the ENTIRE screen — no cursor
        # repositioning, so stray output can never cause duplication.
        self._live = Live(
            console=Console(file=real_stderr_file),
            screen=True,
            refresh_per_second=1,
            get_renderable=self._build,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._live:
            self._live.__exit__(*args)
            self._live = None

        for original_fd, dup_fd in self._saved_fds.items():
            os.dup2(dup_fd, original_fd)
            os.close(dup_fd)
        self._saved_fds.clear()

        if self._devnull_fd >= 0:
            os.close(self._devnull_fd)
            self._devnull_fd = -1

    def set_window(self, window: WalkForwardWindow) -> None:
        self._current_idx = window.index
        self._current_window = window
        self._phase = "hyperopt"
        self._ho_epoch = 0
        self._ho_best = None
        for key in self._phase_log[window.index]:
            self._phase_log[window.index][key] = "pending"
        self._phase_log[window.index]["hyperopt"] = "active"
        self._refresh()

    def set_phase(self, phase: str) -> None:
        if self._current_idx in self._phase_log:
            old = self._phase
            if old in self._phase_log[self._current_idx]:
                self._phase_log[self._current_idx][old] = "done"
            if phase in self._phase_log[self._current_idx]:
                self._phase_log[self._current_idx][phase] = "active"
        self._phase = phase
        self._refresh()

    def on_epoch(self, val: dict[str, Any]) -> None:
        self._ho_epoch = val.get("current_epoch", 0)
        self._ho_total = max(self._ho_total, self._ho_epoch)
        if val.get("is_best"):
            self._ho_best = val
        self._refresh()

    def complete_window(self, result: WindowResult) -> None:
        self._completed.append(result)
        for key in self._phase_log[self._current_idx]:
            self._phase_log[self._current_idx][key] = "done"
        self._refresh()

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._build())

    def _build(self) -> Panel:
        parts: list[Any] = []

        parts.append(self._build_header())
        parts.append(Text(""))
        parts.append(self._build_hyperopt_progress())
        parts.append(Text(""))
        parts.append(self._build_pipeline())

        if self._completed:
            parts.append(Text(""))
            parts.append(self._build_completed_table())
            parts.append(Text(""))
            parts.append(self._build_insights())

        return Panel(
            Group(*parts),
            title=f"[bold]Walk-Forward Analysis[/] — {self._strategy}",
            border_style="cyan",
        )

    def _build_header(self) -> Text:
        elapsed = time.monotonic() - self._start_time
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

        phase_label = PHASE_LABELS.get(self._phase, self._phase)
        line = (
            f"Window {self._current_idx + 1}/{self._n_windows}"
            f"  |  Phase: {phase_label}"
            f"  |  Elapsed: {time_str}"
        )

        if self._current_window:
            w = self._current_window
            line += (
                f"\nTrain: {w.train_start:%Y-%m-%d} -> "
                f"{w.train_end:%Y-%m-%d}"
                f"  |  Test: {w.test_start:%Y-%m-%d} -> "
                f"{w.test_end:%Y-%m-%d}"
            )
        return Text(line)

    def _build_hyperopt_progress(self) -> Group:
        if self._phase != "hyperopt":
            label = PHASE_LABELS.get(self._phase, self._phase)
            return Group(Text(f"  {label}..."))

        best_str = "—"
        if self._ho_best:
            rm = self._ho_best.get("results_metrics", {})
            profit = rm.get("profit_total", 0) * 100
            trades = rm.get("total_trades", 0)
            dd = rm.get("max_drawdown_account", 0) * 100
            loss = self._ho_best.get("loss", 0)
            best_str = f"+{profit:.1f}%  {trades} trades" f"  DD {dd:.1f}%  loss {loss:.5f}"

        info = Text(f"  Hyperopt: {self._ho_epoch}/{self._ho_total}" f"  |  Best: {best_str}")

        pbar = Progress(
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            expand=True,
        )
        task = pbar.add_task("", total=self._ho_total, completed=self._ho_epoch)
        # Force render the progress bar without starting Live
        pbar._tasks[task].completed = self._ho_epoch

        return Group(info, pbar.get_renderable())

    def _build_pipeline(self) -> Table:
        table = Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Pipeline", ratio=1)

        for i in range(self._n_windows):
            phases = self._phase_log.get(i, {})
            parts = []
            for phase_key, label in [
                ("hyperopt", "Hyperopt"),
                ("backtest_optimized", "BT optim"),
                ("backtest_baseline", "BT base"),
            ]:
                status = phases.get(phase_key, "pending")
                if status == "done":
                    parts.append(f"[green]v[/] {label}")
                elif status == "active":
                    parts.append(f"[yellow]>[/] [bold]{label}[/]")
                else:
                    parts.append(f"[dim]o {label}[/]")

            line = f"  Window {i + 1}:  {'   '.join(parts)}"
            table.add_row(line)

        return table

    def _build_completed_table(self) -> Table:
        table = Table(title="Completed Windows", expand=True)
        table.add_column("#", justify="right", width=3)
        table.add_column("Test Period", justify="center")
        table.add_column("Profit", justify="right")
        table.add_column("Trades", justify="right")
        table.add_column("Calmar", justify="right")
        table.add_column("Max DD", justify="right")
        table.add_column("HHI", justify="right")
        table.add_column("Mkt", justify="center")
        table.add_column("Baseline", justify="right")

        for r in self._completed:
            w = r.window
            test_p = r.test_metrics.get("profit_pct", 0)
            trades = r.test_metrics.get("trades", 0)
            calmar = r.test_metrics.get("calmar", 0)
            dd = r.test_metrics.get("max_dd_pct", 0)
            hhi = r.test_metrics.get("hhi", 0)
            base_p = r.baseline_metrics.get("profit_pct", 0)
            regime = r.market_context.get("regime", "")

            profit_style = "green" if test_p > 0 else "red"
            base_style = "green" if base_p > 0 else "red"
            hhi_style = "red" if hhi > 0.15 else "yellow" if hhi > 0.05 else ""

            table.add_row(
                str(w.index + 1),
                f"{w.test_start:%m-%d} -> {w.test_end:%m-%d}",
                Text(f"{test_p:+.1f}%", style=profit_style),
                str(trades),
                f"{calmar:.2f}",
                f"{dd:.1f}%",
                Text(f"{hhi:.3f}", style=hhi_style),
                regime,
                Text(f"{base_p:+.1f}%", style=base_style),
            )

        return table

    def _build_insights(self) -> Panel:
        lines: list[Text] = []
        n = len(self._completed)

        # --- Running verdicts ---
        profits = [r.test_metrics.get("profit_pct", 0) for r in self._completed]
        calmars = [r.test_metrics.get("calmar", 0) for r in self._completed]
        win_count = sum(1 for p in profits if p > 0)
        avg_profit = float(np.mean(profits))
        avg_calmar = float(np.mean(calmars))

        beats_baseline = sum(
            1 for r in self._completed
            if r.test_metrics.get("profit_pct", 0)
            > r.baseline_metrics.get("profit_pct", float("-inf"))
        )

        # Verdict line
        if win_count == n:
            verdict = Text(f"  Verdict: {win_count}/{n} profitable", style="bold green")
        elif win_count >= n / 2:
            verdict = Text(f"  Verdict: {win_count}/{n} profitable", style="bold yellow")
        else:
            verdict = Text(f"  Verdict: {win_count}/{n} profitable", style="bold red")
        lines.append(verdict)

        # Average metrics
        p_style = "green" if avg_profit > 0 else "red"
        c_style = "green" if avg_calmar > 1 else "yellow" if avg_calmar > 0 else "red"
        lines.append(Text(""))
        lines.append(Text.assemble(
            "  Avg profit: ",
            (f"{avg_profit:+.1f}%", p_style),
            "  |  Avg Calmar: ",
            (f"{avg_calmar:.2f}", c_style),
            f"  |  Beats baseline: {beats_baseline}/{n}",
        ))

        # Concentration alerts
        high_hhi = [
            r for r in self._completed
            if r.test_metrics.get("hhi", 0) > 0.15
        ]
        if high_hhi:
            lines.append(Text(
                f"  [!] {len(high_hhi)} window(s) with concentrated"
                f" profit (HHI > 0.15)",
                style="yellow",
            ))

        # Emerging consensus params (top 5 most stable)
        if n >= 2:
            lines.append(Text(""))
            lines.append(Text("  Emerging consensus params:", style="bold"))
            params_table = self._build_param_preview()
            if params_table:
                lines.append(params_table)

        return Panel(
            Group(*lines),
            title="[bold]Insights[/]",
            border_style="blue",
        )

    def _build_param_preview(self) -> Table | None:
        all_params: dict[str, list[float]] = {}
        for r in self._completed:
            for space_params in r.params.values():
                if isinstance(space_params, dict):
                    for k, v in space_params.items():
                        if isinstance(v, int | float):
                            all_params.setdefault(k, []).append(float(v))

        if not all_params:
            return None

        table = Table(
            show_header=True, box=None, padding=(0, 2), expand=True
        )
        table.add_column("Param", style="cyan")
        table.add_column("Median", justify="right")
        table.add_column("Spread", justify="right")
        table.add_column("Stability", justify="center")
        table.add_column("Values", style="dim")

        items = sorted(
            all_params.items(),
            key=lambda kv: np.std(kv[1]) / max(abs(np.mean(kv[1])), 1e-8),
        )

        for name, vals in items[:8]:
            med = float(np.median(vals))
            std = float(np.std(vals))
            mean = float(np.mean(vals))
            cv = std / max(abs(mean), 1e-8)

            if cv < 0.15:
                stab = Text("stable", style="green")
            elif cv < 0.30:
                stab = Text("marginal", style="yellow")
            else:
                stab = Text("unstable", style="red")

            vals_str = ", ".join(f"{v:.2f}" for v in vals)
            table.add_row(
                name,
                f"{med:.4f}",
                f"{std:.4f}",
                stab,
                vals_str,
            )

        return table
