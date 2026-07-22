from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from screener_core import (
    PERIOD_KEYS,
    RANK_BUCKET_CHOICES,
    TIMEFRAMES_DIV,
    format_entry_watches,
    format_results,
    make_exchange,
    run_screen,
    scan_entry_watches,
)

LOCAL_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__),
    ".dashboard_config.json",
)


class ScreenerDashboard:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Coinalyze Futures CVD Screener")
        self.root.geometry("1200x860")
        self.local_config = self._load_local_config()

        self.queue: queue.Queue[tuple[str, str]] = queue.Queue()

        self.side_var = tk.StringVar(value="bull")
        self.mode_var = tk.StringVar(value="continuation")
        self.period_var = tk.StringVar(value="7d")
        self.top_var = tk.StringVar(value="25")
        self.out_var = tk.StringVar(value="20")
        self.min_volume_var = tk.StringVar(value="1000000")
        self.qual_pct_var = tk.StringVar(value="10")
        self.coinalyze_api_key_var = tk.StringVar(
            value=os.getenv("COINALYZE_API_KEY", "")
            or str(self.local_config.get("coinalyze_api_key", ""))
        )
        self.neutral_band_var = tk.StringVar(value="")
        self.no_qual_var = tk.BooleanVar(value=False)
        self.bucket_var = tk.StringVar(value="auto")
        self.ssl_no_verify_var = tk.BooleanVar(
            value=os.getenv("SCREENER_SSL_NO_VERIFY", "0").strip() == "1"
        )
        self.results_sort_var = tk.StringVar(value="default")
        self.entry_sort_var = tk.StringVar(value="closest")

        self.tf_vars: dict[str, tk.BooleanVar] = {
            tf: tk.BooleanVar(value=True) for tf in TIMEFRAMES_DIV
        }

        self._build_ui()
        self._poll_queue()
        self._update_mode_state()
        self._update_command_preview()
        self._update_bucket_help()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(container, text="Controls", padding=10)
        controls.pack(fill="x")

        source_note = ttk.Label(
            controls,
            text="Source: Coinalyze Bybit futures CVD | Universe: Bybit futures",
        )
        source_note.pack(anchor="w", pady=(0, 6))

        row1 = ttk.Frame(controls)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Side").pack(side="left")
        for value, label in (("bull", "Bull"), ("bear", "Bear")):
            ttk.Radiobutton(
                row1,
                text=label,
                value=value,
                variable=self.side_var,
                command=self._on_controls_changed,
            ).pack(side="left", padx=(8, 0))

        ttk.Label(row1, text="Mode").pack(side="left", padx=(18, 0))
        for value, label in (("continuation", "Continuation"), ("reversal", "Reversal")):
            ttk.Radiobutton(
                row1,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_controls_changed,
            ).pack(side="left", padx=(8, 0))

        ttk.Label(row1, text="Period").pack(side="left", padx=(18, 0))
        period_box = ttk.Combobox(
            row1,
            textvariable=self.period_var,
            values=PERIOD_KEYS,
            width=6,
            state="readonly",
        )
        period_box.pack(side="left", padx=(8, 0))
        period_box.bind("<<ComboboxSelected>>", lambda _e: self._on_controls_changed())

        row2 = ttk.Frame(controls)
        row2.pack(fill="x", pady=4)
        self._labeled_entry(row2, "Top", self.top_var, 8)
        self._labeled_entry(row2, "Out", self.out_var, 8)
        self._labeled_entry(row2, "Min Volume", self.min_volume_var, 14)
        self._labeled_entry(row2, "Qual %", self.qual_pct_var, 8)
        self._labeled_entry(row2, "Neutral Band %", self.neutral_band_var, 10)

        row2b = ttk.Frame(controls)
        row2b.pack(fill="x", pady=4)
        self._labeled_entry(
            row2b, "Coinalyze API Key", self.coinalyze_api_key_var, 42, show="*"
        )

        row3 = ttk.Frame(controls)
        row3.pack(fill="x", pady=4)
        ttk.Checkbutton(
            row3,
            text="Disable qualification",
            variable=self.no_qual_var,
            command=self._on_controls_changed,
        ).pack(side="left")
        ttk.Label(row3, text="Bucket").pack(side="left", padx=(16, 0))
        bucket_box = ttk.Combobox(
            row3,
            textvariable=self.bucket_var,
            values=("auto", *RANK_BUCKET_CHOICES),
            width=18,
            state="readonly",
        )
        bucket_box.pack(side="left", padx=(8, 0))
        bucket_box.bind("<<ComboboxSelected>>", lambda _e: self._on_controls_changed())
        ttk.Checkbutton(
            row3,
            text="Use insecure TLS workaround",
            variable=self.ssl_no_verify_var,
            command=self._on_controls_changed,
        ).pack(side="left", padx=(16, 0))

        tf_frame = ttk.LabelFrame(controls, text="Divergence Timeframes", padding=10)
        tf_frame.pack(fill="x", pady=(8, 4))
        for tf in TIMEFRAMES_DIV:
            ttk.Checkbutton(
                tf_frame,
                text=tf,
                variable=self.tf_vars[tf],
                command=self._on_controls_changed,
            ).pack(side="left", padx=(0, 10))

        sort_frame = ttk.LabelFrame(controls, text="Sorting", padding=10)
        sort_frame.pack(fill="x", pady=(8, 4))
        ttk.Label(sort_frame, text="Results").pack(side="left")
        results_sort = ttk.Combobox(
            sort_frame,
            textvariable=self.results_sort_var,
            values=("default", "biggest 7D move", "most TF agreement"),
            width=20,
            state="readonly",
        )
        results_sort.pack(side="left", padx=(8, 16))
        results_sort.bind("<<ComboboxSelected>>", lambda _e: self._on_controls_changed())

        ttk.Label(sort_frame, text="Entry Watch").pack(side="left")
        entry_sort = ttk.Combobox(
            sort_frame,
            textvariable=self.entry_sort_var,
            values=("closest to entry", "strongest reaction %"),
            width=22,
            state="readonly",
        )
        entry_sort.pack(side="left", padx=(8, 0))
        entry_sort.bind("<<ComboboxSelected>>", lambda _e: self._on_controls_changed())

        action_row = ttk.Frame(controls)
        action_row.pack(fill="x", pady=(8, 0))
        ttk.Button(action_row, text="Run Scan", command=self.run_scan).pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(action_row, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        preview_frame = ttk.LabelFrame(container, text="Command Preview", padding=10)
        preview_frame.pack(fill="x", pady=(12, 0))
        self.command_var = tk.StringVar(value="")
        ttk.Label(preview_frame, textvariable=self.command_var).pack(anchor="w")
        self.bucket_help_var = tk.StringVar(value="")
        ttk.Label(preview_frame, textvariable=self.bucket_help_var).pack(anchor="w", pady=(6, 0))

        results_frame = ttk.LabelFrame(container, text="Results", padding=10)
        results_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.output = ScrolledText(results_frame, wrap="none", font=("Consolas", 10))
        self.output.pack(fill="both", expand=True)

    def _load_local_config(self) -> dict[str, str]:
        try:
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_local_config(self) -> None:
        data = {
            "coinalyze_api_key": self.coinalyze_api_key_var.get().strip(),
        }
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def _labeled_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        width: int,
        show: str | None = None,
    ) -> None:
        frame = ttk.Frame(parent)
        frame.pack(side="left", padx=(0, 12))
        ttk.Label(frame, text=label).pack(anchor="w")
        entry = ttk.Entry(frame, textvariable=variable, width=width, show=show)
        entry.pack(anchor="w")
        entry.bind("<KeyRelease>", lambda _e: self._on_controls_changed())

    def _on_controls_changed(self) -> None:
        self._update_mode_state()
        self._update_command_preview()
        self._update_bucket_help()

    def _update_mode_state(self) -> None:
        is_continuation = self.mode_var.get() == "continuation"
        if self.bucket_var.get() != "auto":
            self.no_qual_var.set(False)
        if not is_continuation and self.bucket_var.get() == "auto":
            self.neutral_band_var.set("")
        self.status_var.set("Ready.")

    def _script_name(self) -> str:
        return f"main_{self.side_var.get()}_{self.mode_var.get()}.py"

    def _selected_tfs(self) -> list[str]:
        return [tf for tf, var in self.tf_vars.items() if var.get()]

    def _update_command_preview(self) -> None:
        cmd = ["python", self._script_name(), "--period", self.period_var.get()]
        if self.top_var.get().strip():
            cmd += ["--top", self.top_var.get().strip()]
        if self.out_var.get().strip():
            cmd += ["--out", self.out_var.get().strip()]
        if self.min_volume_var.get().strip():
            cmd += ["--min-volume", self.min_volume_var.get().strip()]
        if self.qual_pct_var.get().strip():
            cmd += ["--qual-pct", self.qual_pct_var.get().strip()]
        if self.coinalyze_api_key_var.get().strip():
            cmd += ["COINALYZE_API_KEY=***set***"]
        if self.no_qual_var.get():
            cmd.append("--no-qual")
        if self.bucket_var.get() != "auto":
            cmd += ["--bucket", self.bucket_var.get()]
        neutral = self.neutral_band_var.get().strip()
        if self.bucket_var.get() == "auto" and self.mode_var.get() == "continuation" and neutral:
            cmd += ["--neutral-band", neutral]
        tfs = self._selected_tfs()
        if tfs:
            cmd += ["--div-tf", *tfs]
        self.command_var.set(" ".join(cmd))

    def _update_bucket_help(self) -> None:
        bucket = self.bucket_var.get()
        qual = self._parse_float(self.qual_pct_var.get(), 10.0)
        neutral = self._parse_float(self.neutral_band_var.get(), 5.0)
        if bucket == "auto":
            self.bucket_help_var.set(
                f"Auto mode: qual={qual:g}%, neutral helper={neutral:g}%."
            )
            return
        if bucket == "strong gainers":
            msg = f"Bucket range: return >= +{qual:g}%."
        elif bucket == "mild gainers":
            msg = f"Bucket range: +{neutral:g}% < return < +{qual:g}%."
        elif bucket == "neutral":
            msg = f"Bucket range: -{neutral:g}% <= return <= +{neutral:g}%."
        elif bucket == "mild losers":
            msg = f"Bucket range: -{qual:g}% < return < -{neutral:g}%."
        elif bucket == "strong losers":
            msg = f"Bucket range: return <= -{qual:g}%."
        else:
            msg = ""
        self.bucket_help_var.set(msg)

    def _parse_float(self, value: str, default: float) -> float:
        try:
            return float(value.strip())
        except Exception:
            return default

    def _parse_int(self, value: str, default: int) -> int:
        try:
            return int(value.strip())
        except Exception:
            return default

    def run_scan(self) -> None:
        tfs = tuple(self._selected_tfs())
        if not tfs:
            self.status_var.set("Select at least one divergence timeframe.")
            return
        self.status_var.set("Running scan...")
        self.output.delete("1.0", "end")
        worker = threading.Thread(target=self._run_scan_worker, args=(tfs,), daemon=True)
        worker.start()

    def _run_scan_worker(self, tfs: tuple[str, ...]) -> None:
        try:
            if self.ssl_no_verify_var.get():
                os.environ["SCREENER_SSL_NO_VERIFY"] = "1"
            else:
                os.environ.pop("SCREENER_SSL_NO_VERIFY", None)
            api_key = self.coinalyze_api_key_var.get().strip()
            if api_key:
                os.environ["COINALYZE_API_KEY"] = api_key
                self._save_local_config()
            elif os.getenv("SCREENER_SOURCE", "coinalyze").strip().lower() == "coinalyze":
                raise ValueError("Enter your Coinalyze API key in the dashboard.")

            ex = make_exchange()
            is_bull = self.side_var.get() == "bull"
            is_continuation = self.mode_var.get() == "continuation"
            rank_losers = is_continuation != is_bull
            div_mode = "bull" if is_bull else "bear"

            neutral_value = None
            neutral_text = self.neutral_band_var.get().strip()
            if self.bucket_var.get() == "auto" and is_continuation and neutral_text:
                neutral_value = self._parse_float(neutral_text, 0.0) / 100.0
            rank_bucket = None if self.bucket_var.get() == "auto" else self.bucket_var.get()

            rows = run_screen(
                ex,
                period=self.period_var.get(),
                rank_losers=rank_losers,
                div_mode=div_mode,
                top_universe=self._parse_int(self.top_var.get(), 25),
                out_limit=self._parse_int(self.out_var.get(), 20),
                min_quote_volume_usdt=self._parse_float(self.min_volume_var.get(), 1_000_000.0),
                universe_scan_limit=300,
                gainer_min_return=self._parse_float(self.qual_pct_var.get(), 10.0) / 100.0,
                loser_max_return=-(self._parse_float(self.qual_pct_var.get(), 10.0) / 100.0),
                use_qualification=not self.no_qual_var.get(),
                neutral_return_band=neutral_value,
                div_timeframes=tfs,
                rank_bucket=rank_bucket,
            )

            qual_note = self._qual_note()
            title = f"{self.side_var.get().upper()} {self.mode_var.get().upper()} | {qual_note} | {self.period_var.get()} + {div_mode} div (Bybit futures CVD)"
            entry_title = (
                "ENTRY WATCH | next HL after bull div"
                if is_bull
                else "ENTRY WATCH | next LH after bear div"
            )
            entry_rows = scan_entry_watches(ex, rows, div_mode, timeframes=tfs)
            rows = self._sort_results(rows)
            entry_rows = self._sort_entry_rows(entry_rows)
            text = format_results(title, rows) + "\n\n" + format_entry_watches(entry_title, entry_rows)
            self.queue.put(("success", text))
        except Exception as exc:
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))

    def _sort_results(self, rows):
        mode = self.results_sort_var.get()
        if mode == "biggest 7D move":
            return sorted(
                rows,
                key=lambda r: abs(r.returns.get("7d") or 0.0),
                reverse=True,
            )
        if mode == "most TF agreement":
            return sorted(
                rows,
                key=lambda r: (r.score, abs(r.returns.get("7d") or 0.0)),
                reverse=True,
            )
        return rows

    def _sort_entry_rows(self, rows):
        mode = self.entry_sort_var.get()
        if mode == "strongest reaction %":
            return sorted(
                rows,
                key=lambda r: (r.reaction_pct, -r.distance_pct),
                reverse=True,
            )
        return sorted(
            rows,
            key=lambda r: (
                r.status not in ("near next HL", "near next LH"),
                r.distance_pct,
                -r.reaction_pct,
            ),
        )

    def _qual_note(self) -> str:
        neutral_text = self.neutral_band_var.get().strip()
        if self.bucket_var.get() != "auto":
            return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, bucket={self.bucket_var.get()}"
        if self.mode_var.get() == "continuation" and neutral_text:
            return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, neutral=+/-{neutral_text}%"
        if self.no_qual_var.get():
            return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, no qual"
        pct = self.qual_pct_var.get().strip() or "10"
        if self.mode_var.get() == "continuation":
            word = "gainers" if self.side_var.get() == "bull" else "losers"
            sign = "+" if self.side_var.get() == "bull" else "-"
            return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, {word}>={sign}{pct}%"
        if self.side_var.get() == "bull":
            return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, losers<=-{pct}%"
        return f"vol>={self._parse_float(self.min_volume_var.get(), 1_000_000.0):,.0f} USDT, gainers>=+{pct}%"

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self.output.delete("1.0", "end")
                self.output.insert("1.0", payload)
                self.status_var.set("Done." if kind == "success" else "Run failed.")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    ScreenerDashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
