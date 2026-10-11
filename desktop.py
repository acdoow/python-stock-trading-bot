"""Trading-agent desktop: offline simulation, paper orders and live controls."""
import os
import queue
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import messagebox, ttk

from adaptive import ROOT, TradingAPI, load_rules
from agent import TradingAgent, load_agent_settings, offline_agent_demo


class App:
    def __init__(self, root):
        defaults = load_rules()
        agent_defaults = load_agent_settings()
        self.root, self.events = root, queue.Queue()
        self.worker, self.stop = None, threading.Event()
        self.mode = tk.StringVar(value="Paper orders")
        self.key = tk.StringVar(value=os.getenv("APCA_API_KEY_ID", ""))
        self.secret = tk.StringVar(value=os.getenv("APCA_API_SECRET_KEY", ""))
        self.account = tk.StringVar()
        self.dollars = tk.StringVar(value=str(defaults.dollars_each))
        self.cap = tk.StringVar(value=str(defaults.capital_cap))
        self.loss = tk.StringVar(value=str(defaults.loss_fraction * 100))
        self.entry_rule = tk.StringVar(value="Momentum growth" if defaults.momentum.enabled else "Configured list")
        self.lookback = tk.StringVar(value=str(defaults.momentum.window_bars * defaults.bar_minutes))
        self.session_gain = tk.StringVar(value=str(defaults.momentum.min_session_gain_fraction * 100))
        self.window_gain = tk.StringVar(value=str(defaults.momentum.min_window_gain_fraction * 100))
        self.live_learning = tk.BooleanVar(value=False)
        self.planner = tk.StringVar(value="Rule agent" if agent_defaults.planner == "rules" else "Local model agent")
        self.model = tk.StringVar(value=agent_defaults.model)
        self.model_url = tk.StringVar(value=agent_defaults.model_url)
        self.status = tk.StringVar(value="Start with the invented-price demo. No account is needed.")
        root.title("StockBot | Trading Agent")
        root.geometry("1080x880")
        root.minsize(980, 780)
        root.configure(bg="#101923")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#101923")
        style.configure("TLabel", background="#101923", foreground="#dbe6ef", font=("Segoe UI", 10))
        style.configure("Title.TLabel", foreground="#f7fbff", font=("Segoe UI", 24, "bold"))
        style.configure("Sub.TLabel", foreground="#9fb1c0")
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
        style.configure("TEntry", fieldbackground="#20303e", foreground="#ffffff", padding=5)
        style.configure("TCombobox", fieldbackground="#20303e", foreground="#ffffff", padding=5)
        style.configure("TCheckbutton", background="#101923", foreground="#dbe6ef", font=("Segoe UI", 10))
        style.configure("Card.TFrame", background="#20303e")
        style.configure("Card.TLabel", background="#20303e", foreground="#dbe6ef")
        style.configure("Value.TLabel", background="#20303e", foreground="#67dfb1", font=("Segoe UI", 18, "bold"))

        outer = ttk.Frame(root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="StockBot Trading Agent", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Observe holdings · Plan permitted buys · Manage exits · Review outcomes",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 16))
        cards = ttk.Frame(outer)
        cards.pack(fill="x")
        self.values = []
        for i, (title, value) in enumerate((("Mode", "PAPER"), ("Portfolio budget", f"${defaults.capital_cap:.2f}"),
                                           ("Plateau window", f"~{defaults.bar_minutes * defaults.plateau.window_bars} minutes"), ("Learning", "Collecting"))):
            box = ttk.Frame(cards, style="Card.TFrame", padding=14)
            box.grid(row=0, column=i, sticky="nsew", padx=(0, 10 if i < 3 else 0))
            cards.columnconfigure(i, weight=1)
            ttk.Label(box, text=title, style="Card.TLabel").pack(anchor="w")
            label = ttk.Label(box, text=value, style="Value.TLabel")
            label.pack(anchor="w", pady=(6, 0))
            self.values.append(label)

        settings = ttk.Frame(outer)
        settings.pack(fill="x", pady=(16, 10))
        self.editable = []
        for i, (label, variable) in enumerate((("Dollars per stock", self.dollars),
                                               ("Total capital cap", self.cap), ("Loss threshold (%)", self.loss))):
            ttk.Label(settings, text=label).grid(row=0, column=i, sticky="w")
            entry = ttk.Entry(settings, textvariable=variable, width=16)
            entry.grid(row=1, column=i, sticky="ew", padx=(0, 14), pady=5)
            self.editable.append(entry)
            settings.columnconfigure(i, weight=1)
        ttk.Label(settings, text="Connection mode").grid(row=0, column=3, sticky="w")
        modes = ttk.Combobox(settings, textvariable=self.mode,
                             values=("Paper orders", "Live preview", "Live orders"), state="readonly", width=19)
        modes.grid(row=1, column=3, sticky="ew", pady=5)
        modes.bind("<<ComboboxSelected>>", self.refresh_mode)
        self.mode_widget = modes
        settings.columnconfigure(3, weight=1)

        growth = ttk.Frame(outer)
        growth.pack(fill="x", pady=(0, 6))
        ttk.Label(growth, text="Buying rule").grid(row=0, column=0, sticky="w")
        self.entry_rule_widget = ttk.Combobox(growth, textvariable=self.entry_rule,
            values=("Momentum growth", "Configured list"), state="readonly", width=19)
        self.entry_rule_widget.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=5)
        self.entry_rule_widget.bind("<<ComboboxSelected>>", self.refresh_entry_rule)
        self.growth_widgets = []
        for i, (label, variable) in enumerate((("Growth lookback (minutes)", self.lookback),
                                               ("Min rise from open (%)", self.session_gain),
                                               ("Min recent rise (%)", self.window_gain)), start=1):
            ttk.Label(growth, text=label).grid(row=0, column=i, sticky="w")
            widget = ttk.Entry(growth, textvariable=variable, width=16)
            widget.grid(row=1, column=i, sticky="ew", padx=(0, 12 if i < 3 else 0), pady=5)
            self.editable.append(widget)
            self.growth_widgets.append(widget)
        for i in range(4):
            growth.columnconfigure(i, weight=1)
        self.refresh_entry_rule()

        brains = ttk.Frame(outer)
        brains.pack(fill="x", pady=(0, 6))
        ttk.Label(brains, text="Agent planner").grid(row=0, column=0, sticky="w")
        self.planner_widget = ttk.Combobox(brains, textvariable=self.planner,
            values=("Rule agent", "Local model agent"), state="readonly", width=19)
        self.planner_widget.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=5)
        self.planner_widget.bind("<<ComboboxSelected>>", self.refresh_planner)
        self.model_widgets = []
        for i, (label, variable) in enumerate((("Local model ID", self.model),
                                              ("Local server URL", self.model_url)), start=1):
            ttk.Label(brains, text=label).grid(row=0, column=i, sticky="w")
            widget = ttk.Entry(brains, textvariable=variable)
            widget.grid(row=1, column=i, sticky="ew", padx=(0, 12 if i == 1 else 0), pady=5)
            self.editable.append(widget)
            self.model_widgets.append(widget)
        for i in range(3):
            brains.columnconfigure(i, weight=2 if i == 2 else 1)
        self.refresh_planner()

        credentials = ttk.Frame(outer)
        credentials.pack(fill="x")
        for i, (label, variable, private) in enumerate((("API key", self.key, True),
                                                        ("API secret", self.secret, True),
                                                        ("Live account UUID", self.account, False))):
            ttk.Label(credentials, text=label).grid(row=0, column=i, sticky="w")
            entry = ttk.Entry(credentials, textvariable=variable, show="•" if private else "")
            entry.grid(row=1, column=i, sticky="ew", padx=(0, 12 if i < 2 else 0), pady=5)
            self.editable.append(entry)
            credentials.columnconfigure(i, weight=1)
        ttk.Label(outer, text="Keys stay in memory on your computer. Paper and live accounts have separate keys.",
                  style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
        self.learning_switch = ttk.Checkbutton(outer, variable=self.live_learning,
            text="Allow evaluated plateau updates for new LIVE trades (budget and loss threshold stay fixed)", state="disabled")
        self.learning_switch.pack(anchor="w")

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(12, 10))
        self.demo_button = ttk.Button(controls, text="Run offline agent demo", command=self.demo)
        self.demo_button.pack(side="left")
        self.start_button = ttk.Button(controls, text="Start paper orders", command=self.start)
        self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(controls, text="Stop agent", command=self.stop_bot, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(controls, text="Setup guide", command=self.guide).pack(side="right")
        ttk.Label(outer, textvariable=self.status, wraplength=1010).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(outer, bg="#0b121a", fg="#bcd0df", insertbackground="white", relief="flat",
                           font=("Consolas", 10), wrap="word", padx=12, pady=10, height=10)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        ttk.Label(outer, text="A detected plateau can be followed by another rise. Targets may not fill. Fractional DAY exits expire each trading day.",
                  style="Sub.TLabel", wraplength=1010).pack(anchor="w", pady=(10, 0))
        ttk.Label(outer, text="The loss threshold is not a guaranteed maximum loss. Stopping leaves orders and holdings in your brokerage account.",
                  style="Sub.TLabel", wraplength=1010).pack(anchor="w", pady=(4, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)

    def refresh_mode(self, event=None):
        mode = self.mode.get()
        self.values[0].configure(text={"Paper orders": "PAPER", "Live preview": "LIVE PREVIEW", "Live orders": "REAL MONEY"}[mode])
        self.start_button.configure(text={"Paper orders": "Start paper orders", "Live preview": "Start live preview", "Live orders": "Start live orders"}[mode])
        self.learning_switch.configure(state="normal" if mode == "Live orders" else "disabled")
        self.status.set("LIVE ORDERS use real money. Review rules, budget and README before starting." if mode == "Live orders"
                        else "Live preview reads account data and sends no orders." if mode == "Live preview"
                        else "Paper orders use virtual money.")

    def rules(self):
        rules = load_rules()
        enabled = self.entry_rule.get() == "Momentum growth"
        momentum = replace(rules.momentum, enabled=enabled)
        if enabled:
            minutes = int(self.lookback.get().strip())
            if minutes % rules.bar_minutes:
                raise ValueError(f"Use a whole growth lookback divisible by {rules.bar_minutes} minutes.")
            momentum = replace(momentum, window_bars=minutes // rules.bar_minutes,
                               min_session_gain_fraction=float(self.session_gain.get()) / 100,
                               min_window_gain_fraction=float(self.window_gain.get()) / 100)
        return replace(rules, dollars_each=float(self.dollars.get()), capital_cap=float(self.cap.get()),
                       loss_fraction=float(self.loss.get()) / 100,
                       auto_adapt_live=self.live_learning.get(), momentum=momentum).validate()

    def refresh_entry_rule(self, event=None):
        busy = self.worker is not None and self.worker.is_alive()
        state = "normal" if self.entry_rule.get() == "Momentum growth" and not busy else "disabled"
        for widget in self.growth_widgets:
            widget.configure(state=state)

    def refresh_planner(self, event=None):
        busy = self.worker is not None and self.worker.is_alive()
        state = "normal" if self.planner.get() == "Local model agent" and not busy else "disabled"
        for widget in self.model_widgets:
            widget.configure(state=state)

    def agent_settings(self):
        return replace(load_agent_settings(),
            planner="local" if self.planner.get() == "Local model agent" else "rules",
            model=self.model.get().strip(), model_url=self.model_url.get().strip()).validate()

    def append(self, value):
        self.log.configure(state="normal")
        self.log.insert("end", str(value) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def busy(self, value):
        self.demo_button.configure(state="disabled" if value else "normal")
        self.start_button.configure(state="disabled" if value else "normal")
        self.stop_button.configure(state="normal" if value else "disabled")
        self.mode_widget.configure(state="disabled" if value else "readonly")
        self.planner_widget.configure(state="disabled" if value else "readonly")
        self.entry_rule_widget.configure(state="disabled" if value else "readonly")
        for widget in self.editable:
            widget.configure(state="disabled" if value else "normal")
        for widget in self.model_widgets:
            widget.configure(state="normal" if not value and self.planner.get() == "Local model agent" else "disabled")
        for widget in self.growth_widgets:
            widget.configure(state="normal" if not value and self.entry_rule.get() == "Momentum growth" else "disabled")
        self.learning_switch.configure(state="normal" if not value and self.mode.get() == "Live orders" else "disabled")

    def launch(self, work):
        if self.worker and self.worker.is_alive():
            return
        self.stop = threading.Event()
        self.busy(True)
        def run():
            try:
                work()
            except Exception as error:
                self.events.put(("error", str(error)))
            finally:
                self.events.put(("done", None))
        self.worker = threading.Thread(target=run, daemon=False)
        self.worker.start()

    def demo(self):
        try:
            rules = self.rules()
        except (ValueError, RuntimeError) as error:
            messagebox.showerror("Check settings", str(error))
            return
        self.status.set("Rule-agent simulation: invented prices and a fake broker. No network or model connection.")
        def work():
            result = offline_agent_demo(rules, ROOT / "agent_demo", emit=lambda line: self.events.put(("log", line)))
            self.events.put(("summary", result))
        self.launch(work)

    def start(self):
        try:
            rules = self.rules()
            agent_settings = self.agent_settings()
            key, secret, account = self.key.get().strip(), self.secret.get().strip(), self.account.get().strip()
            selected = self.mode.get()
            mode, execute = ("paper", True) if selected == "Paper orders" else ("live", selected == "Live orders")
            api = TradingAPI(key, secret, mode=mode, execute=execute)
            folder = ROOT / "results" / (mode + ("" if execute else "-preview"))
            if (folder / "STOP_BOT").exists():
                raise ValueError("STOP_BOT is present in the results folder. Remove it only when ready to restart.")
            if mode == "live" and execute and not account:
                raise ValueError("Enter the exact live account UUID from an authenticated account lookup. Use Live preview first; README explains this.")
        except (ValueError, RuntimeError) as error:
            messagebox.showinfo("Connection settings", str(error))
            return
        self.values[1].configure(text=f"${rules.capital_cap:.2f}")
        entry = (f"Growth filter: {rules.momentum.window_bars * rules.bar_minutes} minutes; waiting is allowed."
                 if rules.momentum.enabled else "Configured-list entry selected.")
        self.status.set(f"{selected} running. {entry} Keep the program awake to manage holdings.")
        def work():
            TradingAgent(api, rules, folder, settings=agent_settings, account_id=account or None, stop=self.stop,
                      emit=lambda line: self.events.put(("log", line))).run()
        self.launch(work)

    def stop_bot(self):
        self.stop.set()
        self.status.set("Stopping after the current request. Existing orders and holdings remain.")

    def guide(self):
        messagebox.showinfo("Getting started",
            "1. Read README.md and run the offline agent demo.\n"
            "2. Create an Alpaca paper account and generate PAPER keys.\n"
            "3. Test Paper orders through complete fills, stops and unfilled targets.\n"
            "4. Live preview uses LIVE keys and sends no orders.\n"
            "5. Live orders needs your exact account UUID and uses REAL MONEY.\n\n"
            "Default: up to $2 each / $10 total. Momentum growth checks rising session prices before buying.\n"
            "The 30-minute growth window needs completed bars; the agent can wait or buy fewer stocks.\n"
            "Existing exits: sell after a 50% drop, or ask the mean price of a near-high plateau.\n"
            "Rule agent needs no language model. Local model agent needs a running loopback server; see SOUP_GUIDE.md.\n"
            "The model only proposes permitted new buys or waits. The engine controls amounts and exits.\n"
            "These rules have not been shown to be profitable. Learning tests exit settings; live updates are off by default.")

    def drain(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "log":
                self.append(value)
            elif kind == "summary":
                self.values[3].configure(text="Collecting")
                self.status.set("Simulation complete. The summary and decision history are in agent_demo.")
            elif kind == "error":
                self.append("PAUSED: " + value)
                self.status.set("Paused. Inspect the log and brokerage dashboard before restarting.")
            elif kind == "done":
                self.busy(False)
                if self.stop.is_set():
                    self.status.set("Stopped. Existing orders and holdings remain in the brokerage account.")
        self.root.after(100, self.drain)

    def close(self):
        self.stop.set()
        if self.worker and self.worker.is_alive():
            self.status.set("Waiting for the in-flight request to finish before closing…")
            self.root.after(100, self.close)
        else:
            self.root.destroy()


if __name__ == "__main__":
    App(tk.Tk()).root.mainloop()
