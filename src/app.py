"""FileBox desktop interface (Tkinter + MySQL + C file-system backend)."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import mysql.connector
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

import database
import os_backend


ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage"
LOGIN_ART = ROOT / "assets" / "filebox_front_page.png"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000)
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def safe_name(value: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise ValueError("Use a folder or file name without path separators.")
    return value


def human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


class FileBox:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("FileBox | Local file manager")
        self.root.geometry("1080x680")
        self.root.minsize(860, 560)
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.db = None
        self.user = None
        self.current_folder_id = None
        self.current_folder_path = ""
        self._style()
        self.show_login()

    def _style(self):
        # Golden Cream palette sampled from the user's dashboard reference.
        self.colors = {
            "page": "#FFFBEA",
            "surface": "#FFFFFF",
            "soft_gold": "#FFF2C6",
            "gold": "#EFB400",
            "gold_hover": "#DFA700",
            "copper": "#B75008",
            "ink": "#2D2A27",
            "muted": "#807970",
            "line": "#EAD99F",
            "selected": "#FFEFB8",
        }
        self.root.configure(background=self.colors["page"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.colors["page"])
        style.configure("Card.TFrame", background=self.colors["surface"])
        style.configure("Title.TLabel", background=self.colors["page"], font=("Segoe UI", 21, "bold"), foreground=self.colors["ink"])
        style.configure("LoginTitle.TLabel", background=self.colors["page"], font=("Segoe UI", 30, "bold"), foreground=self.colors["ink"])
        style.configure("LoginCard.TFrame", background="#FFFEF8")
        style.configure("FieldIcon.TLabel", background=self.colors["surface"], foreground=self.colors["muted"], font=("Segoe UI Symbol", 12))
        style.configure("Login.TEntry", fieldbackground=self.colors["surface"], foreground=self.colors["ink"], borderwidth=0, padding=(1, 3))
        style.configure("LoginOption.TCheckbutton", background="#FFFEF8", foreground=self.colors["muted"], font=("Segoe UI", 9))
        style.map("LoginOption.TCheckbutton", background=[("active", "#FFFEF8")])
        style.configure("Link.TButton", background="#FFFEF8", foreground=self.colors["muted"], padding=(2, 4), font=("Segoe UI", 9, "underline"))
        style.map("Link.TButton", background=[("active", "#FFFEF8")], foreground=[("active", self.colors["copper"])])
        style.configure("Create.TButton", padding=(12, 10), font=("Segoe UI", 10, "bold"), background="#FFFFFF", foreground=self.colors["ink"])
        style.configure("SignIn.TButton", padding=(12, 10), font=("Segoe UI", 10, "bold"), background=self.colors["gold"], foreground="#4B3100")
        style.map("SignIn.TButton", background=[("active", self.colors["gold_hover"])], foreground=[("active", "#4B3100")])
        style.configure("Hint.TLabel", background=self.colors["page"], foreground=self.colors["muted"], font=("Segoe UI", 9))
        style.configure("TLabel", background=self.colors["page"], foreground=self.colors["ink"], font=("Segoe UI", 10))
        style.configure("TButton", padding=(11, 7), font=("Segoe UI", 9), background=self.colors["surface"], foreground=self.colors["ink"])
        style.map("TButton", background=[("active", self.colors["soft_gold"])], foreground=[("active", self.colors["ink"])])
        style.configure("Accent.TButton", padding=(12, 7), font=("Segoe UI", 9, "bold"), background=self.colors["gold"], foreground=self.colors["ink"])
        style.map("Accent.TButton", background=[("active", self.colors["gold_hover"])], foreground=[("active", self.colors["ink"])])
        style.configure("TEntry", fieldbackground=self.colors["surface"], foreground=self.colors["ink"], insertcolor=self.colors["ink"])
        style.configure("TCombobox", fieldbackground=self.colors["surface"], foreground=self.colors["ink"], background=self.colors["surface"])
        style.map("TCombobox", fieldbackground=[("readonly", self.colors["surface"])], selectbackground=[("readonly", self.colors["soft_gold"])])
        style.configure("Treeview", rowheight=32, font=("Segoe UI", 10), background=self.colors["surface"], fieldbackground=self.colors["surface"], foreground=self.colors["ink"])
        style.map("Treeview", background=[("selected", self.colors["selected"])], foreground=[("selected", self.colors["ink"])])
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), background=self.colors["soft_gold"], foreground=self.colors["ink"], padding=(8, 8))
        style.map("Treeview.Heading", background=[("active", self.colors["line"])])

    def run(self):
        self.root.mainloop()

    def toggle_fullscreen(self, _event=None):
        fullscreen = not bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", fullscreen)
        return "break"

    def exit_fullscreen(self, _event=None):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
            return "break"

    def clear(self):
        for child in self.root.winfo_children():
            child.destroy()

    def show_login(self):
        self.clear()
        self.user = None
        self.db = None
        # Keep the login illustration at its natural proportions while allowing
        # the window to maximize or enter borderless fullscreen mode.
        self.root.geometry("672x527")
        self.root.minsize(672, 527)
        self.root.resizable(True, True)

        self.login_canvas = tk.Canvas(self.root, background="#FFF9E6", highlightthickness=0)
        self.login_canvas.pack(fill="both", expand=True)

        card = ttk.Frame(self.login_canvas, style="LoginCard.TFrame", padding=(16, 12))
        card.grid_columnconfigure(0, weight=1)

        self.email_placeholder = False
        self.password_placeholder = False
        self.password_visible = False
        self.remember_var = tk.BooleanVar(value=bool(getattr(self, "remembered_email", "")))

        email_row = tk.Frame(card, background="#FFFFFF", highlightbackground="#E5DED0", highlightthickness=1)
        email_row.grid(row=0, column=0, sticky="ew", pady=(0, 10), ipady=3)
        email_row.grid_columnconfigure(1, weight=1)
        ttk.Label(email_row, text="✉", style="FieldIcon.TLabel", width=4, anchor="center").grid(row=0, column=0, sticky="ns")
        self.email_entry = tk.Entry(email_row, font=("Segoe UI", 10), bg="#FFFFFF", fg=self.colors["ink"],
                                    insertbackground=self.colors["ink"], relief="flat", bd=0, highlightthickness=0)
        self.email_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), ipady=5)
        self._show_email_placeholder()
        self.email_entry.bind("<FocusIn>", self._email_focus_in)
        self.email_entry.bind("<FocusOut>", self._email_focus_out)

        password_row = tk.Frame(card, background="#FFFFFF", highlightbackground="#E5DED0", highlightthickness=1)
        password_row.grid(row=1, column=0, sticky="ew", pady=(0, 10), ipady=3)
        password_row.grid_columnconfigure(1, weight=1)
        ttk.Label(password_row, text="🔒", style="FieldIcon.TLabel", width=4, anchor="center").grid(row=0, column=0, sticky="ns")
        self.password_entry = tk.Entry(password_row, font=("Segoe UI", 10), bg="#FFFFFF", fg=self.colors["ink"],
                                       insertbackground=self.colors["ink"], relief="flat", bd=0, highlightthickness=0, show="")
        self.password_entry.grid(row=0, column=1, sticky="ew", ipady=5)
        self._show_password_placeholder()
        self.password_entry.bind("<FocusIn>", self._password_focus_in)
        self.password_entry.bind("<FocusOut>", self._password_focus_out)
        self.password_eye = tk.Button(password_row, text="\U0001F441", width=2, command=self.toggle_password,
                                      font=("Segoe UI Emoji", 11), bg="#FFFFFF", activebackground="#FFFFFF",
                                      relief="flat", bd=0, highlightthickness=0, cursor="hand2", takefocus=False)
        self.password_eye.grid(row=0, column=2, padx=(2, 5))

        options = ttk.Frame(card, style="LoginCard.TFrame")
        options.grid(row=2, column=0, sticky="ew", pady=(0, 13))
        ttk.Checkbutton(options, text="Remember me", variable=self.remember_var,
                        style="LoginOption.TCheckbutton").pack(side="left")
        ttk.Button(options, text="Forgot password?", style="Link.TButton",
                   command=self.forgot_password).pack(side="right")

        buttons = ttk.Frame(card, style="LoginCard.TFrame")
        buttons.grid(row=3, column=0, sticky="ew")
        for column in (0, 1):
            buttons.grid_columnconfigure(column, weight=1, uniform="login-buttons")
        ttk.Button(buttons, text="Create account", command=self.register,
                   style="Create.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 7))
        ttk.Button(buttons, text="Sign in", command=self.login,
                   style="SignIn.TButton").grid(row=0, column=1, sticky="ew", padx=(7, 0))

        self.login_window = self.login_canvas.create_window(0, 0, window=card, anchor="center", tags=("login-ui",))
        self.login_canvas.bind("<Configure>", self._render_login_scene)
        self._render_login_scene()
        self.root.bind("<Return>", lambda _event: self.login())

    def _render_login_scene(self, _event=None):
        canvas = getattr(self, "login_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        if not hasattr(self, "login_source_image"):
            original = Image.open(LOGIN_ART).convert("RGB")
            # Remove the mock title bar; Tk supplies the real one in windowed mode.
            self.login_source_image = original.crop((14, 124, original.width - 14, original.height - 12))
        canvas.configure(background="#FFF9E6")
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        if width < 20 or height < 20:
            # The initial call can run before Tk maps the window. The canvas
            # Configure binding will draw again when it receives real dimensions.
            return
        scale = min(width / self.login_source_image.width, height / self.login_source_image.height)
        image_size = (max(1, round(self.login_source_image.width * scale)),
                      max(1, round(self.login_source_image.height * scale)))
        # Fill the side margins with a softly blurred, warm extension of the
        # same illustration, then feather the sharp artwork into that backdrop.
        backdrop = ImageOps.fit(self.login_source_image, (width, height), method=Image.Resampling.LANCZOS)
        blur_radius = max(18, round(min(width, height) * 0.055))
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(blur_radius))
        backdrop = Image.blend(backdrop, Image.new("RGB", (width, height), "#FFF9E6"), 0.22)
        artwork = self.login_source_image.resize(image_size, Image.Resampling.LANCZOS)
        image_top = (height - image_size[1]) / 2
        image_left = (width - image_size[0]) / 2
        feather = min(max(12, round(min(image_size) * 0.035)), max(1, min(image_size) // 4))
        mask = Image.new("L", image_size, 0)
        ImageDraw.Draw(mask).rectangle(
            (feather, feather, image_size[0] - feather, image_size[1] - feather), fill=255
        )
        mask = mask.filter(ImageFilter.GaussianBlur(max(1, feather // 2)))
        backdrop.paste(artwork, (round(image_left), round(image_top)), mask)
        self.login_background = ImageTk.PhotoImage(backdrop, master=self.root)
        canvas.delete("background-art")
        canvas.create_image(width / 2, height / 2, image=self.login_background,
                            anchor="center", tags="background-art")
        canvas.tag_lower("background-art")
        if hasattr(self, "login_window"):
            canvas.coords(self.login_window, image_left + 994 * scale, image_top + 1181 * scale)
            canvas.itemconfigure(self.login_window, width=1176 * scale, height=621 * scale)

    def _show_email_placeholder(self):
        remembered = getattr(self, "remembered_email", "")
        if remembered:
            self.email_entry.insert(0, remembered)
            self.email_entry.configure(foreground=self.colors["ink"])
        else:
            self.email_entry.insert(0, "Email")
            self.email_entry.configure(foreground=self.colors["muted"])
            self.email_placeholder = True

    def _email_focus_in(self, _event=None):
        if self.email_placeholder:
            self.email_entry.delete(0, "end")
            self.email_entry.configure(foreground=self.colors["ink"])
            self.email_placeholder = False

    def _email_focus_out(self, _event=None):
        if not self.email_entry.get().strip():
            self.email_entry.insert(0, "Email")
            self.email_entry.configure(foreground=self.colors["muted"])
            self.email_placeholder = True

    def _show_password_placeholder(self):
        self.password_entry.insert(0, "Password")
        self.password_entry.configure(foreground=self.colors["muted"], show="")
        self.password_placeholder = True

    def _password_focus_in(self, _event=None):
        if self.password_placeholder:
            self.password_entry.delete(0, "end")
            self.password_entry.configure(foreground=self.colors["ink"], show="•")
            self.password_placeholder = False

    def _password_focus_out(self, _event=None):
        if not self.password_entry.get():
            self._show_password_placeholder()

    def forgot_password(self):
        messagebox.showinfo("Password help", "Password reset is not configured yet. Contact your project administrator.")

    def toggle_password(self):
        if self.password_placeholder:
            return
        self.password_visible = not self.password_visible
        self.password_entry.configure(show="" if self.password_visible else "•")
        self.password_eye.configure(text="\U0001F441")

    def login(self):
        email = "" if self.email_placeholder else self.email_entry.get().strip().lower()
        password = "" if self.password_placeholder else self.password_entry.get()
        if not email or not password:
            messagebox.showinfo("Log in", "Enter your email and password.")
            return
        try:
            database.initialize()
            db = database.connect()
            cur = db.cursor(dictionary=True)
            cur.execute("SELECT user_id, username, email, password_hash FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            cur.close()
            if not row or not verify_password(password, row["password_hash"]):
                db.close()
                messagebox.showerror("Log in", "Email or password is incorrect.")
                return
            self.remembered_email = email if self.remember_var.get() else ""
            self.db, self.user = db, row
            self.open_workspace()
        except Exception as exc:
            messagebox.showerror("Database connection", f"Could not log in. Check MySQL settings.\n\n{exc}")

    def register(self):
        email = "" if self.email_placeholder else self.email_entry.get().strip().lower()
        password = "" if self.password_placeholder else self.password_entry.get()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            messagebox.showinfo("Create account", "Enter a valid email address.")
            return
        if len(password) < 8:
            messagebox.showinfo("Create account", "Use a password with at least 8 characters.")
            return
        db = None
        try:
            database.initialize()
            db = database.connect()
            cur = db.cursor()
            cur.execute("SELECT user_id FROM users WHERE email=%s", (email,))
            existing = cur.fetchone()
            cur.close()
            if existing:
                db.close()
                messagebox.showinfo("Email already registered", "This email already has an account. Please sign in.")
                return

            username = simpledialog.askstring("Create account", "Your display name:", parent=self.root)
            if not username or not username.strip():
                db.close()
                return

            cur = db.cursor()
            cur.execute("INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                        (username.strip(), email, hash_password(password)))
            db.commit()
            user_id = cur.lastrowid
            cur.close()
            self.db = db
            self.remembered_email = email if self.remember_var.get() else ""
            self.user = {"user_id": user_id, "username": username.strip(), "email": email}
            self.open_workspace()
        except mysql.connector.IntegrityError:
            if db:
                db.rollback()
                db.close()
            messagebox.showinfo("Email already registered", "This email already has an account. Please sign in.")
        except Exception as exc:
            if db:
                db.close()
            messagebox.showerror("Database connection", f"Could not create account. Check MySQL settings.\n\n{exc}")

    def open_workspace(self):
        self.root.unbind("<Return>")
        self.root.geometry("1080x680")
        self.root.minsize(860, 560)
        self.root.resizable(True, True)
        self.clear()
        top = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        top.pack(fill="x")
        ttk.Label(top, text="FileBox", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text=f"Signed in as {self.user['username']}", style="Hint.TLabel").pack(side="left", padx=18, pady=(7, 0))
        ttk.Button(top, text="Log out", command=self.logout).pack(side="right", pady=5)

        actions = ttk.Frame(self.root, padding=(18, 4))
        actions.pack(fill="x")
        for label, command, button_style in (("Upload", self.upload, "Accent.TButton"), ("Replace", self.replace_contents, "TButton"),
                                              ("Download", self.download, "TButton"), ("New folder", self.new_folder, "TButton"),
                                              ("Rename / move", self.rename_move, "TButton"), ("Delete", self.delete_selected, "TButton"),
                                              ("Share", self.share, "TButton")):
            ttk.Button(actions, text=label, command=command, style=button_style).pack(side="left", padx=(0, 7))
        ttk.Label(actions, text="Search").pack(side="left", padx=(18, 6))
        self.search_entry = ttk.Entry(actions, width=25)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Return>", lambda _event: self.refresh_files())
        ttk.Button(actions, text="Find", command=self.refresh_files).pack(side="left", padx=6)
        ttk.Button(actions, text="Clear", command=self.clear_search).pack(side="left")

        folder_bar = ttk.Frame(self.root, padding=(18, 8))
        folder_bar.pack(fill="x")
        ttk.Label(folder_bar, text="Folder").pack(side="left", padx=(0, 8))
        self.folder_combo = ttk.Combobox(folder_bar, state="readonly", width=50)
        self.folder_combo.pack(side="left")
        self.folder_combo.bind("<<ComboboxSelected>>", self.select_folder)
        ttk.Label(folder_bar, text="Shared files are included in search.", style="Hint.TLabel").pack(side="left", padx=12)

        frame = ttk.Frame(self.root, padding=(18, 4, 18, 8))
        frame.pack(fill="both", expand=True)
        columns = ("name", "type", "size", "folder", "owner", "modified")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        widths = {"name": 270, "type": 75, "size": 95, "folder": 245, "owner": 145, "modified": 150}
        for col, title in zip(columns, ("Name", "Type", "Size", "Folder", "Owner", "Modified")):
            self.tree.heading(col, text=title, command=lambda c=col: self.sort_by(c))
            self.tree.column(col, width=widths[col], anchor="w")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.download())

        footer = ttk.Frame(self.root, padding=(18, 5, 18, 14))
        footer.pack(fill="x")
        ttk.Button(footer, text="Recent activity", command=self.show_activity).pack(side="left")
        self.status_label = ttk.Label(footer, text="Ready", style="Hint.TLabel")
        self.status_label.pack(side="left", padx=14)
        ttk.Label(footer, text="Local storage  ·  MySQL metadata", style="Hint.TLabel").pack(side="right")
        self.load_folders()
        self.refresh_files()

    def logout(self):
        if self.db:
            self.db.close()
        self.show_login()

    def close(self):
        if self.db:
            self.db.close()
        self.root.destroy()

    def load_folders(self):
        cur = self.db.cursor(dictionary=True)
        cur.execute("SELECT folder_id, folder_name, relative_path FROM folders WHERE owner_id=%s ORDER BY relative_path",
                    (self.user["user_id"],))
        self.folders = cur.fetchall()
        cur.close()
        self.folder_by_label = {"All files": (None, "")}
        for folder in self.folders:
            self.folder_by_label[folder["relative_path"]] = (folder["folder_id"], folder["relative_path"])
        values = list(self.folder_by_label)
        self.folder_combo["values"] = values
        label = self.current_folder_path if self.current_folder_path in self.folder_by_label else "All files"
        self.folder_combo.set(label)
        self.current_folder_id, self.current_folder_path = self.folder_by_label[label]

    def select_folder(self, _event=None):
        self.current_folder_id, self.current_folder_path = self.folder_by_label[self.folder_combo.get()]
        self.search_entry.delete(0, "end")
        self.refresh_files()

    def refresh_files(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        term = self.search_entry.get().strip()
        user_id = self.user["user_id"]
        cur = self.db.cursor(dictionary=True)
        if term:
            cur.execute("""SELECT DISTINCT f.file_id, f.owner_id, f.file_name, f.file_type, f.file_size,
                       f.relative_path, f.modified_at, owner.username AS owner_name
                       FROM files f JOIN users owner ON owner.user_id=f.owner_id
                       LEFT JOIN permissions p ON p.file_id=f.file_id
                       WHERE (f.owner_id=%s OR p.grantee_user_id=%s) AND f.file_name LIKE %s
                       ORDER BY f.file_name""", (user_id, user_id, f"%{term}%"))
        elif self.current_folder_path:
            cur.execute("""SELECT f.file_id, f.owner_id, f.file_name, f.file_type, f.file_size,
                       f.relative_path, f.modified_at, owner.username AS owner_name
                       FROM files f JOIN users owner ON owner.user_id=f.owner_id
                       WHERE f.owner_id=%s AND f.folder_id=%s ORDER BY f.file_name""",
                        (user_id, self.current_folder_id))
        else:
            cur.execute("""SELECT DISTINCT f.file_id, f.owner_id, f.file_name, f.file_type, f.file_size,
                       f.relative_path, f.modified_at, owner.username AS owner_name
                       FROM files f JOIN users owner ON owner.user_id=f.owner_id
                       LEFT JOIN permissions p ON p.file_id=f.file_id
                       WHERE (f.owner_id=%s OR p.grantee_user_id=%s) ORDER BY f.file_name""", (user_id, user_id))
        self.rows = cur.fetchall()
        cur.close()
        self.rows_by_iid = {}
        for row in self.rows:
            folder = str(Path(row["relative_path"]).parent).replace("\\", "/")
            if folder == ".": folder = "Root"
            modified = row["modified_at"].strftime("%Y-%m-%d %H:%M") if row["modified_at"] else ""
            iid = self.tree.insert("", "end", values=(row["file_name"], row["file_type"] or "File",
                                human_size(row["file_size"]), folder, row["owner_name"], modified))
            self.rows_by_iid[iid] = row
        self.status_label.configure(text=f"{len(self.rows)} file{'s' if len(self.rows) != 1 else ''} shown")

    def selected(self):
        chosen = self.tree.selection()
        if not chosen:
            messagebox.showinfo("FileBox", "Select a file first.")
            return None
        return self.rows_by_iid[chosen[0]]

    def can_write(self, row):
        if row["owner_id"] == self.user["user_id"]:
            return True
        cur = self.db.cursor()
        cur.execute("SELECT permission_type FROM permissions WHERE file_id=%s AND grantee_user_id=%s",
                    (row["file_id"], self.user["user_id"]))
        value = cur.fetchone()
        cur.close()
        return bool(value and value[0] == "WRITE")

    def audit(self, action, file_id=None, details=""):
        database.log_activity(self.db, self.user["user_id"], file_id, action, details)
        self.db.commit()
        if hasattr(self, "status_label"):
            self.status_label.configure(text=f"{action.replace('_', ' ').title()} complete")

    def upload(self):
        source = filedialog.askopenfilename(title="Choose a file to add to FileBox")
        if not source: return
        name = safe_name(Path(source).name)
        relative = f"{self.current_folder_path}/{name}" if self.current_folder_path else name
        destination = STORAGE / str(self.user["user_id"]) / Path(relative)
        if destination.exists():
            messagebox.showerror("Upload", "A file with that name already exists in this folder.")
            return
        try:
            os_backend.import_file(STORAGE / str(self.user["user_id"]), source, relative)
            size = os_backend.file_size(STORAGE / str(self.user["user_id"]), relative)
            extension = Path(name).suffix.lower().lstrip(".")[:32]
            cur = self.db.cursor()
            cur.execute("INSERT INTO files (owner_id, folder_id, file_name, relative_path, file_type, file_size) VALUES (%s,%s,%s,%s,%s,%s)",
                        (self.user["user_id"], self.current_folder_id, name, relative.replace("\\", "/"), extension, size))
            file_id = cur.lastrowid
            database.log_activity(self.db, self.user["user_id"], file_id, "UPLOAD", name)
            self.db.commit()
            self.refresh_files()
        except Exception as exc:
            self.db.rollback()
            try:
                os_backend.delete_file(STORAGE / str(self.user["user_id"]), relative)
            except OSError:
                pass
            messagebox.showerror("Upload", str(exc))

    def download(self):
        row = self.selected()
        if not row: return
        target = filedialog.asksaveasfilename(initialfile=row["file_name"], title="Save a copy")
        if not target: return
        try:
            os_backend.export_file(STORAGE / str(row["owner_id"]), row["relative_path"], target)
            self.audit("DOWNLOAD", row["file_id"], row["file_name"])
        except Exception as exc:
            messagebox.showerror("Download", str(exc))

    def new_folder(self):
        name = simpledialog.askstring("New folder", "Folder name:", parent=self.root)
        if not name: return
        try:
            name = safe_name(name)
            relative = f"{self.current_folder_path}/{name}" if self.current_folder_path else name
            os_backend.make_directory(STORAGE / str(self.user["user_id"]), relative)
            parent_id = self.current_folder_id
            cur = self.db.cursor()
            cur.execute("INSERT INTO folders (owner_id,parent_folder_id,folder_name,relative_path) VALUES (%s,%s,%s,%s)",
                        (self.user["user_id"], parent_id, name, relative))
            self.db.commit()
            self.load_folders()
            self.folder_combo.set(relative)
            self.select_folder()
            self.audit("CREATE_FOLDER", details=relative)
        except Exception as exc:
            self.db.rollback()
            messagebox.showerror("New folder", str(exc))

    def rename_move(self):
        row = self.selected()
        if not row: return
        if not self.can_write(row):
            messagebox.showerror("Permission denied", "Only the owner or a user with WRITE permission can change this file.")
            return
        new_name = simpledialog.askstring("Rename / move", "New file name:", initialvalue=row["file_name"], parent=self.root)
        if not new_name: return
        try:
            new_name = safe_name(new_name)
            if row["owner_id"] != self.user["user_id"]:
                messagebox.showerror("Permission denied", "Shared WRITE permission allows file contents to be updated; only the owner can rename or move it.")
                return
            folder_names = ["(root)"] + [item["relative_path"] for item in self.folders]
            destination_folder = simpledialog.askstring("Rename / move", "Destination folder path (blank for root):\n\nAvailable: " + ", ".join(folder_names), parent=self.root)
            if destination_folder is None: return
            destination_folder = destination_folder.strip().replace("\\", "/").strip("/")
            allowed = {item["relative_path"] for item in self.folders}
            if destination_folder and destination_folder not in allowed:
                raise ValueError("Choose a folder shown in the available folder list.")
            new_rel = f"{destination_folder}/{new_name}" if destination_folder else new_name
            new_folder_id = next((item["folder_id"] for item in self.folders if item["relative_path"] == destination_folder), None)
            os_backend.move_file(STORAGE / str(self.user["user_id"]), row["relative_path"], new_rel)
            cur = self.db.cursor()
            cur.execute("UPDATE files SET file_name=%s, relative_path=%s, folder_id=%s, file_type=%s WHERE file_id=%s",
                        (new_name, new_rel, new_folder_id, Path(new_name).suffix.lower().lstrip(".")[:32], row["file_id"]))
            database.log_activity(self.db, self.user["user_id"], row["file_id"], "RENAME", f"{row['file_name']} -> {new_name}")
            self.db.commit()
            self.refresh_files()
        except Exception as exc:
            self.db.rollback()
            messagebox.showerror("Rename / move", str(exc))

    def replace_contents(self):
        row = self.selected()
        if not row: return
        if not self.can_write(row):
            messagebox.showerror("Permission denied", "Only the owner or a user with WRITE permission can replace this file.")
            return
        source = filedialog.askopenfilename(title="Choose replacement file")
        if not source: return
        if Path(source).name != row["file_name"] and not messagebox.askyesno(
            "Replace contents", f"The selected file is named {Path(source).name}. Replace the contents of {row['file_name']} with it?"):
            return
        try:
            os_backend.import_file(STORAGE / str(row["owner_id"]), source, row["relative_path"])
            size = os_backend.file_size(STORAGE / str(row["owner_id"]), row["relative_path"])
            cur = self.db.cursor()
            cur.execute("UPDATE files SET file_size=%s, file_type=%s WHERE file_id=%s",
                        (size, Path(row["file_name"]).suffix.lower().lstrip(".")[:32], row["file_id"]))
            database.log_activity(self.db, self.user["user_id"], row["file_id"], "REPLACE", row["file_name"])
            self.db.commit()
            self.refresh_files()
        except Exception as exc:
            self.db.rollback()
            messagebox.showerror("Replace contents", str(exc))

    def delete_selected(self):
        row = self.selected()
        if not row: return
        if row["owner_id"] != self.user["user_id"]:
            messagebox.showerror("Permission denied", "Only the owner can delete a file.")
            return
        if not messagebox.askyesno("Delete file", f"Permanently delete {row['file_name']} from FileBox?"):
            return
        try:
            os_backend.delete_file(STORAGE / str(self.user["user_id"]), row["relative_path"])
            cur = self.db.cursor()
            cur.execute("DELETE FROM files WHERE file_id=%s AND owner_id=%s", (row["file_id"], self.user["user_id"]))
            database.log_activity(self.db, self.user["user_id"], None, "DELETE", row["file_name"])
            self.db.commit()
            self.refresh_files()
        except Exception as exc:
            self.db.rollback()
            messagebox.showerror("Delete", str(exc))

    def share(self):
        row = self.selected()
        if not row: return
        if row["owner_id"] != self.user["user_id"]:
            messagebox.showerror("Share", "Only the owner can change sharing permissions.")
            return
        email = simpledialog.askstring("Share file", "Share with this user's email:", parent=self.root)
        if not email: return
        permission = simpledialog.askstring("Share file", "Permission: READ or WRITE", initialvalue="READ", parent=self.root)
        permission = (permission or "").strip().upper()
        if permission not in {"READ", "WRITE"}:
            messagebox.showerror("Share", "Permission must be READ or WRITE.")
            return
        try:
            cur = self.db.cursor()
            cur.execute("SELECT user_id FROM users WHERE email=%s", (email.strip().lower(),))
            recipient = cur.fetchone()
            if not recipient:
                messagebox.showerror("Share", "No FileBox account has that email.")
                return
            if recipient[0] == self.user["user_id"]:
                messagebox.showinfo("Share", "This file already belongs to you.")
                return
            cur.execute("INSERT INTO permissions (file_id,grantee_user_id,permission_type) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE permission_type=VALUES(permission_type)",
                        (row["file_id"], recipient[0], permission))
            database.log_activity(self.db, self.user["user_id"], row["file_id"], "SHARE", f"{email}: {permission}")
            self.db.commit()
            messagebox.showinfo("Share", f"Shared with {email} as {permission}.")
            self.refresh_files()
        except Exception as exc:
            self.db.rollback()
            messagebox.showerror("Share", str(exc))

    def show_activity(self):
        cur = self.db.cursor(dictionary=True)
        cur.execute("SELECT action,details,created_at FROM activity_log WHERE user_id=%s ORDER BY created_at DESC LIMIT 100",
                    (self.user["user_id"],))
        rows = cur.fetchall()
        cur.close()
        window = tk.Toplevel(self.root)
        window.title("Recent activity")
        window.geometry("720x420")
        view = ttk.Treeview(window, columns=("action", "details", "time"), show="headings")
        for col, label, width in (("action", "Action", 130), ("details", "Details", 390), ("time", "Time", 160)):
            view.heading(col, text=label); view.column(col, width=width, anchor="w")
        view.pack(fill="both", expand=True, padx=12, pady=12)
        for row in rows:
            created = row["created_at"].strftime("%Y-%m-%d %H:%M") if row["created_at"] else ""
            view.insert("", "end", values=(row["action"], row["details"], created))

    def clear_search(self):
        self.search_entry.delete(0, "end")
        self.refresh_files()

    def sort_by(self, column):
        mapped = {"name": "file_name", "type": "file_type", "size": "file_size", "modified": "modified_at"}
        key = mapped.get(column)
        if not key: return
        self.rows.sort(key=lambda row: row.get(key) or "", reverse=getattr(self, "sort_reverse", False))
        self.sort_reverse = not getattr(self, "sort_reverse", False)
        for item in self.tree.get_children(): self.tree.delete(item)
        self.rows_by_iid = {}
        for row in self.rows:
            folder = str(Path(row["relative_path"]).parent).replace("\\", "/")
            if folder == ".": folder = "Root"
            modified = row["modified_at"].strftime("%Y-%m-%d %H:%M") if row["modified_at"] else ""
            iid = self.tree.insert("", "end", values=(row["file_name"], row["file_type"] or "File", human_size(row["file_size"]), folder, row["owner_name"], modified))
            self.rows_by_iid[iid] = row


if __name__ == "__main__":
    app = FileBox()
    app.root.protocol("WM_DELETE_WINDOW", app.close)
    app.run()
