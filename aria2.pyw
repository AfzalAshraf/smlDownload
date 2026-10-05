import os
import sys
import re
import time
import subprocess
import threading
import urllib.request
import urllib.parse
import json
import shutil
import zipfile
import customtkinter as ctk
from tkinter import filedialog

# Global Visual Theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


class Aria2GuiApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # --- Window Settings ---
        self.title("aria2 & Cloud Stream Download Manager")
        self.geometry("880x900")
        self.minsize(760, 740)

        if sys.platform == "win32":
            self.aria2_folder = r"C:\aria2"
        else:
            self.aria2_folder = os.path.expanduser("~/.aria2_bin")

        self.output_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.active_process = None
        self.is_cancelled = False

        # Session & cURL Storage
        self.imported_headers = {}
        self.imported_post_data = None
        self.http_method = "GET"

        self.grid_rowconfigure(5, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ==========================================
        # 1. Header Section
        # ==========================================
        self.header_frame = ctk.CTkFrame(self, corner_radius=10)
        self.header_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nsew")
        self.header_frame.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text="aria2 & Cloud Stream Download Manager",
            font=ctk.CTkFont(size=22, weight="bold")
        )
        self.title_label.grid(row=0, column=0, padx=20, pady=(15, 2), sticky="w")

        self.subtitle_label = ctk.CTkLabel(
            self.header_frame,
            text=f"Engine Path: {self.aria2_folder}",
            font=ctk.CTkFont(size=12, slant="italic"),
            text_color="gray70"
        )
        self.subtitle_label.grid(row=1, column=0, padx=20, pady=(0, 15), sticky="w")

        self.update_btn = ctk.CTkButton(
            self.header_frame,
            text="🔄 Install / Update aria2",
            command=self.start_update_thread,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a",
            border_width=1,
            border_color="#555555"
        )
        self.update_btn.grid(row=0, column=1, rowspan=2, padx=20, pady=15, sticky="e")

        # ==========================================
        # 2. Output Path & Parameters
        # ==========================================
        self.settings_frame = ctk.CTkFrame(self, corner_radius=10)
        self.settings_frame.grid(row=1, column=0, padx=20, pady=5, sticky="nsew")
        self.settings_frame.grid_columnconfigure(1, weight=1)

        self.path_label = ctk.CTkLabel(
            self.settings_frame,
            text="Save Folder:",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.path_label.grid(row=0, column=0, padx=(15, 5), pady=(12, 6), sticky="w")

        self.path_entry = ctk.CTkEntry(self.settings_frame)
        self.path_entry.grid(row=0, column=1, padx=5, pady=(12, 6), sticky="ew")
        self.path_entry.insert(0, self.output_dir)

        self.browse_btn = ctk.CTkButton(
            self.settings_frame,
            text="📁 Browse",
            width=90,
            command=self.browse_destination_folder,
            fg_color="#1f538d",
            hover_color="#14375e"
        )
        self.browse_btn.grid(row=0, column=2, padx=(5, 15), pady=(12, 6), sticky="e")

        self.opts_frame = ctk.CTkFrame(self.settings_frame, fg_color="transparent")
        self.opts_frame.grid(row=1, column=0, columnspan=3, padx=15, pady=(4, 12), sticky="ew")

        self.conn_label = ctk.CTkLabel(self.opts_frame, text="Conns (-x):", font=ctk.CTkFont(size=12))
        self.conn_label.pack(side="left", padx=(0, 5))
        self.conn_combo = ctk.CTkComboBox(self.opts_frame, values=["1", "2", "4", "8", "16"], width=70)
        self.conn_combo.set("16")
        self.conn_combo.pack(side="left", padx=(0, 20))

        self.split_label = ctk.CTkLabel(self.opts_frame, text="Splits (-s):", font=ctk.CTkFont(size=12))
        self.split_label.pack(side="left", padx=(0, 5))
        self.split_combo = ctk.CTkComboBox(self.opts_frame, values=["1", "2", "4", "8", "16"], width=70)
        self.split_combo.set("16")
        self.split_combo.pack(side="left")

        # ==========================================
        # 3. Authentication & cURL Importer
        # ==========================================
        self.auth_frame = ctk.CTkFrame(self, corner_radius=10)
        self.auth_frame.grid(row=2, column=0, padx=20, pady=5, sticky="nsew")
        self.auth_frame.grid_columnconfigure(1, weight=1)

        self.curl_btn = ctk.CTkButton(
            self.auth_frame,
            text="📋 Import from Browser cURL",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#6A0DAD",
            hover_color="#7B1FA2",
            command=self.open_curl_importer
        )
        self.curl_btn.grid(row=0, column=0, padx=(15, 10), pady=10, sticky="w")

        self.header_status_label = ctk.CTkLabel(
            self.auth_frame,
            text="No active browser session (Public GET mode)",
            font=ctk.CTkFont(size=12),
            text_color="gray70"
        )
        self.header_status_label.grid(row=0, column=1, padx=(5, 15), pady=10, sticky="w")

        # ==========================================
        # 4. Input Links Section
        # ==========================================
        self.input_frame = ctk.CTkFrame(self, corner_radius=10)
        self.input_frame.grid(row=3, column=0, padx=20, pady=5, sticky="nsew")
        self.input_frame.grid_columnconfigure((0, 1), weight=1)

        self.input_label = ctk.CTkLabel(
            self.input_frame,
            text="Paste Download Links (One per line):",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.input_label.grid(row=0, column=0, columnspan=2, padx=15, pady=(10, 5), sticky="w")

        self.links_textbox = ctk.CTkTextbox(self.input_frame, height=100, activate_scrollbars=True)
        self.links_textbox.grid(row=1, column=0, columnspan=2, padx=15, pady=(0, 10), sticky="ew")

        self.download_btn = ctk.CTkButton(
            self.input_frame,
            text="🚀 Start Downloads",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.start_download_thread
        )
        self.download_btn.grid(row=2, column=0, padx=(15, 5), pady=(0, 12), sticky="ew")

        self.cancel_btn = ctk.CTkButton(
            self.input_frame,
            text="⏹ Stop",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#8B0000",
            hover_color="#A52A2A",
            state="disabled",
            command=self.cancel_downloads
        )
        self.cancel_btn.grid(row=2, column=1, padx=(5, 15), pady=(0, 12), sticky="ew")

        # ==========================================
        # 5. Activity Log Section
        # ==========================================
        self.output_frame = ctk.CTkFrame(self, corner_radius=10)
        self.output_frame.grid(row=5, column=0, padx=20, pady=(5, 20), sticky="nsew")
        self.output_frame.grid_rowconfigure(1, weight=1)
        self.output_frame.grid_columnconfigure(0, weight=1)

        self.output_header_frame = ctk.CTkFrame(self.output_frame, fg_color="transparent")
        self.output_header_frame.grid(row=0, column=0, padx=15, pady=(8, 4), sticky="ew")
        self.output_header_frame.grid_columnconfigure(0, weight=1)

        self.output_label = ctk.CTkLabel(
            self.output_header_frame,
            text="Activity Console Log:",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.output_label.grid(row=0, column=0, sticky="w")

        self.clear_btn = ctk.CTkButton(
            self.output_header_frame,
            text="Clear Log",
            width=75,
            height=24,
            command=self.clear_log,
            fg_color="#2b2b2b",
            hover_color="#3a3a3a"
        )
        self.clear_btn.grid(row=0, column=1, sticky="e")

        self.console_textbox = ctk.CTkTextbox(
            self.output_frame,
            fg_color="#121212",
            text_color="#00FF66",
            font=ctk.CTkFont(family="Consolas", size=12),
            activate_scrollbars=True
        )
        self.console_textbox.grid(row=1, column=0, padx=15, pady=(0, 12), sticky="nsew")

    # --------------------------------------------------------------------------
    # Robust Clean cURL Importer (Windows & Linux compatible)
    # --------------------------------------------------------------------------
    def open_curl_importer(self):
        curl_window = ctk.CTkToplevel(self)
        curl_window.title("Paste Browser cURL")
        curl_window.geometry("700x440")
        curl_window.transient(self)
        curl_window.grab_set()

        lbl = ctk.CTkLabel(
            curl_window,
            text="Paste the entire 'Copy as cURL' snippet from Developer Tools below:",
            font=ctk.CTkFont(size=12, weight="bold")
        )
        lbl.pack(padx=20, pady=(15, 5), anchor="w")

        tb = ctk.CTkTextbox(curl_window, height=260, font=ctk.CTkFont(family="Consolas", size=11))
        tb.pack(padx=20, pady=5, fill="both", expand=True)

        def parse_and_apply():
            raw = tb.get("1.0", "end-1c").strip()
            if not raw:
                curl_window.destroy()
                return

            # 1. Clean line continuation escapes (^ on Windows cmd, \ on bash, ` on PS)
            cleaned = re.sub(r'[\^\\`]\r?\n\s*', ' ', raw)
            
            # 2. Extract URL and strip trailing carets/quotes
            url_match = re.search(r"curl(?:[.]exe)?\s+[\^\"']*([^\s\"'\^]+)[\^\"']*", cleaned, re.IGNORECASE)
            if not url_match or not url_match.group(1).startswith("http"):
                url_match = re.search(r"[\"'](https?://[^\s\"']+)[\"']", cleaned)

            target_url = None
            if url_match:
                target_url = url_match.group(1).rstrip('^').strip('"').strip("'")

            # 3. Extract Headers
            headers_dict = {}
            header_matches = re.finditer(r"(?:-H|--header)\s+[\^\"']*([^\r\n\"']+)[\^\"']*", cleaned)
            for m in header_matches:
                header_line = m.group(1).replace('^"', '"').replace('^&', '&').rstrip('^').strip()
                if ":" in header_line:
                    k, v = header_line.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    if not k.startswith(":") and k.lower() != "accept-encoding":
                        headers_dict[k] = v

            # 4. Extract POST Body Payload
            post_data = None
            post_match = re.search(r"(?:--data-raw|--data|-d|--data-binary)\s+[\^\"']*(.*?)[\^\"']*(?:\s+-[A-Za-z]|\s*$)", cleaned, re.DOTALL)
            if post_match:
                post_raw = post_match.group(1).strip()
                post_data = post_raw.replace('^"', '"').replace('\\"', '"').replace('^^', '^').rstrip('^')

            # 5. Determine Method
            if post_data or "--data" in cleaned or "-X POST" in cleaned or (target_url and "transform/zip" in target_url):
                self.http_method = "POST"
            else:
                self.http_method = "GET"

            # Apply parameters
            if target_url:
                self.links_textbox.delete("1.0", "end")
                self.links_textbox.insert("1.0", target_url)
                self.log(f"[cURL Importer] Extracted Clean URL: {target_url[:70]}...")

            self.imported_headers = headers_dict
            self.imported_post_data = post_data

            if headers_dict:
                has_cookie = any(k.lower() == "cookie" for k in headers_dict)
                status_txt = f"Active Session: {self.http_method} Mode | {'Cookie Detected ✅' if has_cookie else 'Headers Active'}"
                self.header_status_label.configure(text=status_txt, text_color="#00FF66")
                self.log(f"[cURL Importer] Imported {len(headers_dict)} headers ({self.http_method} Stream Ready).")
            else:
                self.header_status_label.configure(text="No headers detected", text_color="orange")

            curl_window.destroy()

        apply_btn = ctk.CTkButton(
            curl_window,
            text="Extract & Apply Session",
            command=parse_and_apply,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#1f538d"
        )
        apply_btn.pack(padx=20, pady=(10, 15), fill="x")

    def log(self, text: str):
        clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        self.after(0, self._append_log, clean_text)

    def _append_log(self, text: str):
        self.console_textbox.insert("end", f"{text}\n")
        self.console_textbox.see("end")

    def clear_log(self):
        self.console_textbox.delete("1.0", "end")

    def browse_destination_folder(self):
        selected_dir = filedialog.askdirectory(initialdir=self.output_dir)
        if selected_dir:
            self.output_dir = os.path.normpath(selected_dir)
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, self.output_dir)
            self.log(f"[System] Destination folder set to: {self.output_dir}")

    def start_update_thread(self):
        self.update_btn.configure(state="disabled")
        threading.Thread(target=self.run_aria2_update, daemon=True).start()

    def run_aria2_update(self):
        self.log("[System] Checking for aria2 releases...")
        os.makedirs(self.aria2_folder, exist_ok=True)

        zip_name = "aria2-1.37.0-win-64bit-build1.zip"
        zip_path = os.path.join(self.aria2_folder, zip_name)

        urls_to_try = [
            "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip",
            "https://ghproxy.net/https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
        ]

        success = False
        for url in urls_to_try:
            try:
                self.log(f"[Download] Fetching engine from: {url}")
                req = urllib.request.Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as response, open(zip_path, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)

                if zipfile.is_zipfile(zip_path):
                    success = True
                    break
            except Exception as e:
                self.log(f"[Network Trace] Download node failed: {e}")

        if not success:
            self.log("[Error] Unable to download aria2 automatically.")
            self.after(0, lambda: self.update_btn.configure(state="normal"))
            return

        try:
            self.log("[System] Extracting binaries...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for member in zip_ref.namelist():
                    filename = os.path.basename(member)
                    if filename:
                        source = zip_ref.open(member)
                        target = open(os.path.join(self.aria2_folder, filename), "wb")
                        with source, target:
                            shutil.copyfileobj(source, target)

            if os.path.exists(zip_path):
                os.remove(zip_path)

            self.log(f"[Success] aria2 installed successfully in: {self.aria2_folder}")
        except Exception as e:
            self.log(f"[Error] Extraction failed: {e}")

        self.after(0, lambda: self.update_btn.configure(state="normal"))

    def _locate_aria2(self):
        exe_name = "aria2c.exe" if sys.platform == "win32" else "aria2c"
        direct_path = os.path.join(self.aria2_folder, exe_name)
        if os.path.exists(direct_path):
            return direct_path

        for root, _, files in os.walk(self.aria2_folder):
            if exe_name in files:
                return os.path.join(root, exe_name)

        return shutil.which(exe_name)

    def start_download_thread(self):
        raw_text = self.links_textbox.get("1.0", "end-1c").strip()
        # Clean any trailing carets on the input
        urls = [line.strip().rstrip('^') for line in raw_text.split("\n") if line.strip() and not line.strip().startswith("#")]

        if not urls:
            self.log("[System] Error: Please enter valid download URLs.")
            return

        target_dir = self.path_entry.get().strip()
        if target_dir:
            self.output_dir = os.path.normpath(target_dir)

        self.is_cancelled = False
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        threading.Thread(target=self.dispatch_downloads, args=(urls,), daemon=True).start()

    def cancel_downloads(self):
        self.is_cancelled = True
        self.log("[System] Cancelling active download...")
        if self.active_process and self.active_process.poll() is None:
            try:
                self.active_process.terminate()
            except Exception as e:
                self.log(f"[Warning] Termination error: {e}")

    def dispatch_downloads(self, urls):
        os.makedirs(self.output_dir, exist_ok=True)

        for idx, url in enumerate(urls, 1):
            if self.is_cancelled:
                break

            # If the URL is a dynamic SharePoint zip endpoint or has a POST payload, route through Native Streaming Engine
            if "transform/zip" in url or self.http_method == "POST" or self.imported_post_data:
                self.log("=" * 60)
                self.log(f"[Engine Selection] Routing Task {idx} to Cloud Stream Engine (POST Stream)")
                self.log("=" * 60)
                self.run_python_stream_download(url, idx, len(urls))
            else:
                self.log("=" * 60)
                self.log(f"[Engine Selection] Routing Task {idx} to aria2 16x Multi-Connection Engine")
                self.log("=" * 60)
                self.run_aria2_download_single(url, idx, len(urls))

        self._reset_download_ui()

    # --------------------------------------------------------------------------
    # Engine A: Cloud Stream Native Engine (For Microsoft POST /transform/zip)
    # --------------------------------------------------------------------------
    def run_python_stream_download(self, url, current_idx, total_items):
        try:
            self.log(f"[Stream Engine] Connecting to dynamic cloud endpoint...")
            req = urllib.request.Request(url, method=self.http_method)

            # Inject session headers
            for k, v in self.imported_headers.items():
                req.add_header(k, v)

            if 'User-Agent' not in self.imported_headers:
                req.add_header('User-Agent', DEFAULT_USER_AGENT)

            # Inject JSON POST Payload
            if self.imported_post_data:
                req.data = self.imported_post_data.encode('utf-8')
                if not any(k.lower() == 'content-type' for k in self.imported_headers):
                    req.add_header('Content-Type', 'application/json;charset=UTF-8')

            with urllib.request.urlopen(req, timeout=30) as resp:
                # Resolve true filename
                cd = resp.headers.get('Content-Disposition', '')
                filename = None
                if cd:
                    fname_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, re.IGNORECASE)
                    if fname_match:
                        filename = urllib.parse.unquote(fname_match.group(1).strip())

                if not filename:
                    filename = "SharePoint_Download.zip" if "transform/zip" in url else "downloaded_file.bin"

                dest_path = os.path.join(self.output_dir, filename)
                base, ext = os.path.splitext(dest_path)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = f"{base}_{counter}{ext}"
                    counter += 1

                total_size = int(resp.headers.get('Content-Length', 0))
                self.log(f"[Stream Engine] Saving File: {os.path.basename(dest_path)}")

                downloaded = 0
                chunk_size = 1024 * 512  # 512 KB buffer
                start_time = time.time()
                last_log = start_time

                with open(dest_path, 'wb') as f:
                    while not self.is_cancelled:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_log >= 1.0:
                            speed = (downloaded / (now - start_time)) / (1024 * 1024)
                            if total_size > 0:
                                percent = (downloaded / total_size) * 100
                                mb_down = downloaded / (1024 * 1024)
                                mb_tot = total_size / (1024 * 1024)
                                self.log(f"[{percent:.1f}%] {mb_down:.2f}/{mb_tot:.2f} MB | Speed: {speed:.2f} MB/s")
                            else:
                                mb_down = downloaded / (1024 * 1024)
                                self.log(f"[Streaming Live] {mb_down:.2f} MB received | Speed: {speed:.2f} MB/s")
                            last_log = now

                if self.is_cancelled:
                    self.log("[System] Stream cancelled. Removing incomplete file...")
                    if os.path.exists(dest_path):
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass
                else:
                    self.log(f"\n[Success] Task {current_idx}/{total_items} finished! File saved: {os.path.basename(dest_path)} ({downloaded / (1024*1024):.2f} MB)")

        except urllib.error.HTTPError as e:
            self.log(f"[HTTP Error {e.code}] {e.reason}")
            if e.code == 405:
                self.log("[Hint] The session token expired. Please re-copy cURL from DevTools.")
            elif e.code in [401, 403]:
                self.log("[Hint] Authentication expired or missing. Please click 'Import from Browser cURL'.")
        except Exception as e:
            self.log(f"[Stream Engine Error] {e}")

    # --------------------------------------------------------------------------
    # Engine B: aria2 High-Speed Multi-Connection Engine (For Static GET URLs)
    # --------------------------------------------------------------------------
    def run_aria2_download_single(self, url, current_idx, total_items):
        executable = self._locate_aria2()
        if not executable:
            self.log("[Error] 'aria2c' executable not found. Click 'Install / Update' first.")
            return

        max_conn = self.conn_combo.get()
        splits = self.split_combo.get()

        self.log(f"[Task {current_idx}/{total_items}] aria2 connecting: {url[:70]}...")

        cmd = [
            executable,
            f"--dir={self.output_dir}",
            f"-x{max_conn}",
            f"-s{splits}",
            "--enable-color=false",
            "--content-disposition=true",
            "--check-certificate=false",
            f"--user-agent={DEFAULT_USER_AGENT}",
            "--summary-interval=1",
            "--console-log-level=notice",
            "--file-allocation=none",
            "--allow-overwrite=true"
        ]

        for k, v in self.imported_headers.items():
            cmd.append(f"--header={k}: {v}")

        cmd.append(url)

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        try:
            self.active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags
            )

            for line in iter(self.active_process.stdout.readline, ''):
                if self.is_cancelled:
                    break
                clean = line.strip()
                if clean:
                    self.log(clean)

            self.active_process.stdout.close()
            self.active_process.wait()

            if self.is_cancelled:
                self.log("[System] Queue stopped by user.")
            elif self.active_process.returncode == 0:
                self.log(f"[Success] Item {current_idx} completed successfully.")
            else:
                self.log(f"[Warning] Item {current_idx} ended with exit code: {self.active_process.returncode}")

        except Exception as e:
            self.log(f"[Error] Execution failure: {e}")
        finally:
            self.active_process = None

    def _reset_download_ui(self):
        self.after(0, lambda: self.download_btn.configure(state="normal"))
        self.after(0, lambda: self.cancel_btn.configure(state="disabled"))


if __name__ == "__main__":
    app = Aria2GuiApp()
    app.mainloop()