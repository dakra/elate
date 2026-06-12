;;; elate-agent.el --- in-Emacs agent for elate sessions  -*- lexical-binding: t; -*-

;;; Commentary:

;; This file is loaded into every elate-managed Emacs.  It starts a
;; per-session server.el socket inside the session sandbox and provides
;; the RPC entry points the elate controller calls via `emacsclient
;; --eval'.
;;
;; Every RPC response is a base64-encoded UTF-8 JSON string.  This
;; sidesteps emacsclient's printing/escaping quirks entirely: the
;; controller only ever has to strip the surrounding quotes from a
;; string containing nothing but the base64 alphabet.

;;; Code:

(require 'server)
(require 'backtrace)
(require 'json)
(require 'seq)
(require 'subr-x)
(require 'help-fns)
(require 'ert)
(require 'profiler)

(defvar internal-when-entered-debugger)  ; eval.c (Emacs 28+)

;; Bound by `elate--lint-byte-compile' / `elate--lint-checkdoc'; the
;; defining libraries are required at runtime, not at compile time.
(defvar byte-compile-dest-file-function)
(defvar byte-compile-verbose)
(defvar byte-compile-log-buffer)
(defvar checkdoc-autofix-flag)
(defvar checkdoc-diagnostic-buffer)
(defvar checkdoc-create-error-function)

;; package.el is required at runtime by `elate-clean-install' (only
;; clean-install sessions pay for loading it).
(defvar package-alist)
(defvar package-user-dir)
(declare-function package-buffer-info "package")
(declare-function package-dir-info "package")
(declare-function package-tar-file-info "package")
(declare-function package-install-file "package")
(declare-function package-installed-p "package")
(declare-function package-version-join "package")
(declare-function package-desc-name "package")
(declare-function package-desc-reqs "package")
(declare-function package-desc-version "package")
(declare-function package-desc-dir "package")
(declare-function tar-mode "tar-mode")
(declare-function dired-mode "dired")

(defvar elate--clean-installed nil
  "Plists describing the packages `elate-clean-install' installed.
Most recent first; each is (:name :version :dir :warnings).")

(defvar elate-session-dir nil
  "Absolute path of this session's sandbox directory.
Set via --eval / generated init.el before this file is loaded.")

(defun elate--call-sans-debugger (fn &rest args)
  "Call FN on ARGS with the interactive error debugger disabled.
Installed as around-advice on server.el's request-handling entry
points: an error signalled there -- typically `server-send-string'
answering a client socket whose emacsclient was killed by a
controller-side timeout -- must never invoke the debugger inside the
process filter.  With `debug-on-error' non-nil (the minimal config
default) the debugger's recursive edit would block all further request
processing and permanently wedge the semantic channel.  RPC backtrace
capture is unaffected: `elate-rpc' re-binds `debug-on-error' around
its own handlers."
  (let ((debug-on-error nil)
        (debug-on-quit nil))
    (apply fn args)))

(defun elate-agent-init ()
  "Start the per-session server.el socket inside the sandbox."
  (unless elate-session-dir
    (error "elate-session-dir is not set"))
  (setq server-name "elate"
        server-use-tcp nil
        server-socket-dir (expand-file-name "server" elate-session-dir)
        server-client-instructions nil)
  ;; server.el requires the socket dir to exist with safe permissions.
  (make-directory server-socket-dir t)
  (set-file-modes server-socket-dir #o700)
  ;; Shield every server.el request-handling entry point from the
  ;; debugger: the filter itself, the deferred-pending path, and the
  ;; goto-toplevel continuation (the latter two also run from timers).
  (dolist (fn '(server-process-filter
                server--process-filter-all-pending
                server-execute-continuation))
    (when (fboundp fn)
      (advice-add fn :around #'elate--call-sans-debugger)))
  (server-start))

(defun elate--record-init-error (err)
  "Append ERR to the sandbox's init-error file for the controller to find."
  (when elate-session-dir
    (let ((inhibit-message t))
      (write-region (concat (error-message-string err) "\n") nil
                    (expand-file-name "init-error" elate-session-dir)
                    'append 'silent))))

(defmacro elate-guard (&rest body)
  "Evaluate BODY; record any error to the session's init-error file.
Used around user-supplied startup forms so a failing --eval/--load or
init file is reported by `elate start' instead of silently dropping the
session into the debugger."
  `(condition-case elate--guard-err
       (progn ,@body)
     (error (elate--record-init-error elate--guard-err) nil)))

;;;; Encoding helpers

(defun elate--clean-string (s)
  "S with every character `json-serialize' rejects replaced by U+FFFD.
Elisp strings may legally contain surrogate code points (e.g. from
\(string #xD800)) and raw eight-bit characters (any buffer visiting a
file with invalid UTF-8).  `json-serialize' signals on both, so every
string placed in an RPC payload is scrubbed here; otherwise a single
binary buffer would make `state'/`buffer' fail and the resulting error
text (containing the same bytes) would poison the echo area and every
subsequent snapshot."
  (let* ((s (if (multibyte-string-p s) s (string-to-multibyte s)))
         (len (length s))
         (i 0)
         (dirty nil))
    (while (and (< i len) (not dirty))
      (let ((c (aref s i)))
        (setq dirty (or (<= #xD800 c #xDFFF) (< #x10FFFF c))
              i (1+ i))))
    (if (not dirty)
        s                               ; common case: no copy
      (mapconcat (lambda (c)
                   (if (or (<= #xD800 c #xDFFF) (< #x10FFFF c))
                       "�"
                     (char-to-string c)))
                 s ""))))

(defun elate--clean (obj)
  "Deep copy of OBJ with every string passed through `elate--clean-string'."
  (cond ((stringp obj) (elate--clean-string obj))
        ((consp obj) (mapcar #'elate--clean obj))
        ((vectorp obj) (vconcat (mapcar #'elate--clean obj)))
        (t obj)))

(defun elate--encode (obj)
  "Serialize OBJ (a plist) to JSON and base64-encode it.
Strings are scrubbed of non-Unicode characters first, and an encoding
failure is itself converted into a structured error payload, so a
reply can never escape as a raw `json-serialize' error."
  (base64-encode-string
   (encode-coding-string
    (condition-case err
        (json-serialize (elate--clean obj))
      (error
       (json-serialize
        (list :ok :false
              :error (elate--clean-string
                      (format "elate: cannot encode RPC payload: %s"
                              (error-message-string err)))
              :backtrace :null))))
    'utf-8 t)
   t))

(defun elate--decode-string (b64)
  "Decode a base64-encoded UTF-8 string B64."
  (decode-coding-string (base64-decode-string b64) 'utf-8))

(defun elate--jnull (x)
  "X, or :null when X is nil (JSON null)."
  (or x :null))

(defun elate--jbool (x)
  "X as a JSON boolean: t or :false."
  (if x t :false))

(defun elate--clip-string (s limit)
  "S truncated to LIMIT characters with a marker appended, or S itself."
  (if (and (stringp s) (> (length s) limit))
      (concat (substring s 0 limit)
              (format "...[truncated, %d chars total]" (length s)))
    s))

;;;; RPC dispatcher

(defun elate--rearm-debugger ()
  "Allow the next error to invoke the debugger (i.e. our capture) again.
Entering the debugger normally suppresses re-entry until new user
input arrives; RPC traffic produces no input events, so without this
reset only the first error per input-event window would be captured."
  (when (boundp 'internal-when-entered-debugger)
    (setq internal-when-entered-debugger -1)))

(defun elate-rpc (fn &rest args)
  "Dispatch RPC call FN (a string) with ARGS; return base64 JSON.
Errors are reported as {ok: false, error, backtrace}."
  (elate--encode
   (let ((bt nil))
     (letrec ((capture
               (lambda (&rest _args)
                 (elate--rearm-debugger)
                 (unless bt
                   (setq bt (elate--backtrace-string
                             capture
                             (lambda (fr)
                               (eq (backtrace-frame-fun fr) 'elate-rpc))))))))
       ;; `debug-on-error' is re-enabled here so the `(debug error)'
       ;; handlers reliably invoke CAPTURE: the server-filter advice
       ;; disables it, and bare (-Q) configs never had it.
       (let ((debugger capture)
             (debug-on-error t))
         (condition-case err
             (let ((sym (intern-soft (concat "elate--rpc-" fn))))
               (unless (and sym (fboundp sym))
                 (error "elate: unknown RPC function %S" fn))
               (list :ok t :data (apply sym args)))
           ((debug error) (list :ok :false
                                :error (error-message-string err)
                                :backtrace (elate--jnull bt)))))))))

;;;; Introspection helpers

(defun elate--current-buffer ()
  "The buffer a user would consider current: the selected window's buffer."
  (window-buffer (selected-window)))

(defun elate--minibuffer-completions ()
  "Completion candidates plist for the current minibuffer, or nil.
Capped at 50 candidates; :truncated says whether more exist.  Never
signals: a broken completion table must not take `state' down."
  (condition-case nil
      (when minibuffer-completion-table
        (let* ((contents (minibuffer-contents-no-properties))
               (comps (completion-all-completions
                       contents
                       minibuffer-completion-table
                       minibuffer-completion-predicate
                       (length contents)))
               (out nil)
               (n 0))
          ;; COMPS is an improper list (the last cdr is the base size).
          (while (and (consp comps) (< n 50))
            (push (substring-no-properties (car comps)) out)
            (setq comps (cdr comps)
                  n (1+ n)))
          (list :candidates (vconcat (nreverse out))
                :truncated (elate--jbool (consp comps)))))
    (error nil)))

(defun elate--minibuffer-info ()
  "Plist describing the active minibuffer, or :null."
  (let ((win (active-minibuffer-window)))
    (if (not win)
        :null
      (with-current-buffer (window-buffer win)
        (append
         (list :prompt (elate--jnull (minibuffer-prompt))
               :contents (minibuffer-contents-no-properties)
               :depth (minibuffer-depth))
         (let ((comp (elate--minibuffer-completions)))
           (and comp (list :completions comp))))))))

(defun elate--minor-mode-variable (sym)
  "The state variable of minor mode SYM, or nil when it cannot be resolved.
Usually the variable is SYM itself; a mode defined with a :variable
keyword (e.g. `auto-fill-mode', whose state lives in the variable
`auto-fill-function') is resolved through the variable's
`:minor-mode-function' property."
  (if (boundp sym)
      sym
    (let (var)
      (dolist (entry minor-mode-alist)
        (let ((v (car-safe entry)))
          (when (and (not var) (symbolp v)
                     (eq (get v :minor-mode-function) sym))
            (setq var v))))
      var)))

(defun elate--active-minor-modes (buf)
  "Vector of enabled minor mode names in BUF."
  (with-current-buffer buf
    (let (modes)
      (dolist (m minor-mode-list)
        (let ((var (elate--minor-mode-variable m)))
          (when (and var (boundp var) (symbol-value var))
            (push (symbol-name m) modes))))
      (vconcat (nreverse modes)))))

(defun elate--backtrace-string (&optional base cut-pred)
  "Render the current backtrace as a string, or nil on failure.
BASE, if non-nil, is the innermost function whose callees to discard
\(its own frame is dropped too).  Frames at and below the first frame
satisfying CUT-PRED are dropped as elate machinery."
  (condition-case nil
      (let* ((frames (backtrace-get-frames (or base 'elate--backtrace-string)))
             (frames (if base (cdr frames) frames))
             (cut (and cut-pred (seq-position frames nil
                                              (lambda (fr _) (funcall cut-pred fr)))))
             (frames (if cut (seq-take frames cut) frames))
             (print-length 50)
             (print-level 8))
        (backtrace-to-string frames))
    (error nil)))

;;;; RPC functions

(defun elate--rpc-ping ()
  "Liveness probe: answers as long as the command loop services the server."
  (list :pong t))

(defun elate--rpc-emacs-info ()
  "Version, pid, and system type, for session registration."
  (list :version emacs-version
        :pid (emacs-pid)
        :system-type (symbol-name system-type)))

(defvar elate--max-window-text 32768
  "Cap on the visible-text payload of a single window in `state'.
A normal TTY window is a few KiB; very long truncated lines could
otherwise balloon the snapshot (window-end spans the whole logical
line, not just the visible columns).  `elate--rpc-state' rebinds this
downward when many windows share `elate--state-text-budget'.")

(defconst elate--state-text-budget 131072
  "Total visible-text budget across all windows of one `state' snapshot.
emacsclient's print path moves roughly 50 KB/s; without a global
budget, N windows x 32 KiB of truncated long lines would push a single
snapshot toward the controller's RPC timeout (measured: 5 windows =
160 KB = 3.8 s).")

(defun elate--window-info (win)
  "Plist describing window WIN: buffer, geometry, point, mode line, visible text."
  (let ((buf (window-buffer win)))
    (with-current-buffer buf
      (save-excursion
        (let* ((wp (window-point win))
               ;; Clamp to buffer bounds: window-start can be stale (no
               ;; redisplay yet after the buffer shrank).
               (start (min (max (window-start win) (point-min)) (point-max)))
               (end (min (max (or (window-end win t) start) start) (point-max)))
               (text (buffer-substring-no-properties start end))
               (truncated (> (length text) elate--max-window-text)))
          (goto-char wp)
          (list :buffer (buffer-name)
                :selected (elate--jbool (eq win (selected-window)))
                :width (window-total-width win)
                :height (window-total-height win)
                :line (line-number-at-pos wp t)
                :column (current-column)
                :start-line (line-number-at-pos start t)
                :end-line (line-number-at-pos end t)
                :mode-line (substring-no-properties
                            (format-mode-line mode-line-format nil win))
                :text (if truncated
                          (substring text 0 elate--max-window-text)
                        text)
                :text-truncated (elate--jbool truncated)))))))

(defun elate--layout-node (node)
  "Recursively render NODE of `window-tree' as a plist.
Leaves are window plists; internal nodes carry :split
\(\"vertical\" = windows stacked top-to-bottom, \"horizontal\" =
side by side) and a :children vector."
  (if (windowp node)
      (elate--window-info node)
    (list :split (if (car node) "vertical" "horizontal")
          :children (vconcat (mapcar #'elate--layout-node (cddr node))))))

(defun elate--messages-tail (n)
  "Last N lines of *Messages*."
  (with-current-buffer (messages-buffer)
    (save-restriction
      (widen)
      (save-excursion
        (goto-char (point-max))
        (forward-line (- n))
        (buffer-substring-no-properties (point) (point-max))))))

(defun elate--rpc-state ()
  "One-call snapshot of the full interactive scene.
Everything an outside driver needs for situational awareness: the
current buffer and its modes/point/region/narrowing, the window layout
tree with per-window visible text and mode lines, echo area, active
minibuffer (prompt, input, completion candidates), pending input, last
command, and a *Messages* tail."
  (let* ((buf (elate--current-buffer))
         (idle (current-idle-time))
         ;; Split the snapshot's total text budget across windows so a
         ;; many-window frame of long-line buffers cannot push the payload
         ;; past what the emacsclient pipe moves within the RPC timeout.
         (elate--max-window-text
          (max 4096 (min elate--max-window-text
                         (/ elate--state-text-budget
                            (max 1 (length (window-list nil 'no-minibuf))))))))
    (with-current-buffer buf
      (append
       (when elate--clean-installed
         (list :clean-install
               (list :package-user-dir
                     (elate--jnull (and (boundp 'package-user-dir)
                                        package-user-dir))
                     :packages (vconcat
                                (mapcar (lambda (p) (plist-get p :name))
                                        (reverse elate--clean-installed))))))
       (list :buffer (buffer-name)
            :file (elate--jnull (buffer-file-name))
            :point (point)
            :line (line-number-at-pos (point) t)
            :column (current-column)
            :buffer-size (buffer-size)
            :modified (elate--jbool (buffer-modified-p))
            :narrowed (elate--jbool (buffer-narrowed-p))
            :mark (elate--jnull (mark t))
            :region (if (region-active-p)
                        (list :start (region-beginning)
                              :end (region-end)
                              :size (- (region-end) (region-beginning)))
                      :null)
            :major-mode (symbol-name major-mode)
            :minor-modes (elate--active-minor-modes buf)
            :echo (elate--jnull (current-message))
            :minibuffer (elate--minibuffer-info)
            :minibuffer-depth (minibuffer-depth)
            :input-pending (elate--jbool (input-pending-p))
            :unread (length unread-command-events)
            ;; Clipped: last-command can be an anonymous closure (e.g.
            ;; transient suffixes), whose raw printout is multi-KB and
            ;; would bloat every later snapshot.
            :last-command (if last-command (elate--clip-print last-command) :null)
            :idle (if idle (float-time idle) :null)
            :popups (vconcat (mapcar (lambda (p) (plist-get p :kind))
                                     (elate--popups nil)))
            :windows (elate--layout-node (car (window-tree)))
            :messages-tail (elate--messages-tail 10))))))

(defun elate--rpc-buffer (&optional name from to props)
  "Contents of buffer NAME (default: current), lines FROM..TO (1-based, inclusive).
With PROPS non-nil, additionally return run-length-encoded
face/text-property runs and the overlays for the same range (see
`elate--prop-runs' / `elate--overlay-dump'); font-lock is ensured on
the range first so a never-displayed buffer is still fontified."
  (let ((buf (if (and name (stringp name))
                 (or (get-buffer name)
                     (error "elate: no buffer named %S" name))
               (elate--current-buffer))))
    (with-current-buffer buf
      (save-excursion
        (save-restriction
          (widen)
          (let* ((total (line-number-at-pos (point-max) t))
                 (beg (if (and from (numberp from))
                          (progn (goto-char (point-min))
                                 (forward-line (1- from))
                                 (point))
                        (point-min)))
                 (end (if (and to (numberp to))
                          (progn (goto-char (point-min))
                                 (forward-line to)
                                 (point))
                        (point-max)))
                 (lo (min beg end))
                 (hi (max beg end)))
            (append
             (list :name (buffer-name)
                   :text (buffer-substring-no-properties lo hi)
                   :total-lines total)
             (when props
               (when (and font-lock-mode (fboundp 'font-lock-ensure))
                 (ignore-errors (font-lock-ensure lo hi)))
               (append (list :props (elate--prop-runs lo hi))
                       (elate--overlay-dump lo hi))))))))))

(defun elate--rpc-messages (&optional cursor)
  "Tail of *Messages* since CURSOR (a char position); returns new cursor.
A CURSOR beyond the current buffer end (e.g. after truncation) resets
to the beginning."
  (with-current-buffer (messages-buffer)
    (save-restriction
      (widen)
      (let* ((max (point-max))
             (start (if (and (numberp cursor)
                             (>= cursor (point-min))
                             (<= cursor max))
                        cursor
                      (point-min))))
        (list :text (buffer-substring-no-properties start max)
              :cursor max)))))

(defconst elate--max-value-len 65536
  "Cap on the printed length of an eval result.
emacsclient's print path moves roughly 50 KB/s, so an uncapped
multi-megabyte value would blow the controller's subprocess timeout and
masquerade as a busy/blocked Emacs.  Truncated results carry
:truncated t and the full :value-length.")

(defun elate--rpc-eval (form-b64 &optional timeout)
  "Evaluate the elisp source decoded from FORM-B64.
Returns printed value (truncated at `elate--max-value-len'), *Messages*
delta, and error + backtrace on failure.  TIMEOUT (seconds) arms a
`with-timeout' guard; note that it can only fire if the evaluated code
reaches a timer-servicing point."
  (let* ((src (elate--decode-string form-b64))
         (form (read (concat "(progn\n" src "\n)")))
         (msg-start (with-current-buffer (messages-buffer)
                      (save-restriction (widen) (point-max))))
         (backtrace nil)
         (value nil)
         (errstr nil))
    (letrec ((capture
              (lambda (&rest _args)
                (elate--rearm-debugger)
                (unless backtrace
                  (setq backtrace
                        (elate--backtrace-string
                         capture
                         (lambda (fr)
                           (let ((fun (backtrace-frame-fun fr)))
                             (or (eq fun 'elate--rpc-eval)
                                 (and (eq fun 'eval)
                                      (equal (backtrace-frame-args fr)
                                             (list form t))))))))))))
      (let ((debugger capture))
        (condition-case err
          (setq value
                (let ((print-length 4096)
                      (print-level 64))
                  (prin1-to-string
                   (if (and (numberp timeout) (> timeout 0))
                       (with-timeout (timeout (error "elate: eval timed out after %gs" timeout))
                         (eval form t))
                     (eval form t)))))
          ((debug error) (setq errstr (error-message-string err))))))
    (let* ((vlen (if value (length value) 0))
           (truncated (> vlen elate--max-value-len)))
      (list :value (elate--jnull (if truncated
                                     (substring value 0 elate--max-value-len)
                                   value))
            :truncated (elate--jbool truncated)
            :value-length vlen
            :error (elate--jnull errstr)
            :backtrace (elate--jnull backtrace)
            :messages (with-current-buffer (messages-buffer)
                        (save-restriction
                          (widen)
                          (buffer-substring-no-properties
                           (min msg-start (point-max)) (point-max))))))))

(defun elate--rpc-keys (keys &optional method)
  "Deliver KEYS (an Emacs kbd string) semantically.
METHOD is \"macro\" (default; `execute-kbd-macro', synchronous) or
\"events\" (append to `unread-command-events'; asynchronous, processed
when control returns to the command loop -- use this for sequences that
leave a prompt open)."
  (let ((vec (kbd keys)))
    (pcase (or method "macro")
      ("events"
       (setq unread-command-events
             (nconc unread-command-events
                    (listify-key-sequence vec)))
       (list :delivered "events" :keys keys))
      ("macro"
       (execute-kbd-macro vec)
       (list :delivered "macro" :keys keys))
      (other (error "elate: unknown key delivery method %S" other)))))

(defun elate--rpc-type (text-b64)
  "Queue the text decoded from TEXT-B64 (base64 UTF-8) like typing it.
The GUI replacement for raw tmux typing: the text goes onto
`unread-command-events', so each character runs through the command
loop (auto-indent, minibuffer submission, ...), but unlike raw
terminal bytes this needs a responsive Emacs.  Newlines are delivered
as RET (?\\r), matching what a keyboard sends."
  (let ((keys (listify-key-sequence
               (subst-char-in-string ?\n ?\r
                                     (elate--decode-string text-b64)))))
    (setq unread-command-events (nconc unread-command-events keys))
    (list :queued (length keys) :delivered "events")))

(defun elate--rpc-resize (cols rows)
  "Live-resize the selected GUI frame to COLS x ROWS characters."
  (unless (display-graphic-p)
    (error "elate: agent-side resize is GUI-only; TTY sessions resize through tmux"))
  (set-frame-size (selected-frame) cols rows)
  ;; The window system applies the resize asynchronously; give redisplay
  ;; a chance so the reported size is usually the settled one.
  (redisplay t)
  (list :width (frame-width) :height (frame-height)))

(defun elate--rpc-frame-parameter (name)
  "Value of frame parameter NAME on the selected frame, printed.
Used e.g. to resolve the X11 window id (outer-window-id) for
screenshots."
  (let ((value (frame-parameter nil (intern name))))
    (list :name name :value (if value (format "%s" value) :null))))

;;;; Mouse synthesis
;;
;; Mouse events are synthesized inside Emacs: build a real posn at the
;; target (posn-at-point / posn-at-x-y) and dispatch a complete event
;; sequence through the command loop, so whatever bindings a human
;; click would trigger are triggered here too -- buttons, follow-link,
;; mode-line maps, mwheel.  Works identically for TTY and GUI frames
;; and needs no OS-level permissions.

(defun elate--mouse-resolve-pos (win target)
  "Buffer position in WIN's buffer described by TARGET (an alist).
Line/column targets count within the buffer's accessible (narrowed)
region, 1-based for lines; line 0 behaves like line 1.  Out-of-range
pos/line/col clamp to the nearest valid position."
  (with-current-buffer (window-buffer win)
    (let ((pos (alist-get 'pos target))
          (line (alist-get 'line target))
          (col (alist-get 'col target)))
      (cond
       (pos (max (point-min) (min pos (point-max))))
       (line (save-excursion
               (goto-char (point-min))
               (forward-line (1- line))
               (when col (move-to-column col))
               (point)))
       (t (window-point win))))))

(defun elate--mouse-posn (win target)
  "A posn in WIN at the place TARGET describes.
TARGET is an alist with either part=\"mode-line\" (+ optional col, a
character offset into the mode line) or a buffer location (pos, or
line + col; default: window-point).  Buffer positions must be visible
in WIN -- scrolling on the caller's behalf would be a side effect."
  (if (equal (alist-get 'part target) "mode-line")
      (let* ((frame (window-frame win))
             (edges (window-pixel-edges win))
             (col (or (alist-get 'col target) 1))
             (x (min (+ (nth 0 edges) (* col (frame-char-width frame)))
                     (- (nth 2 edges) 1)))
             (y (- (nth 3 edges) 1))
             (posn (posn-at-x-y x y frame t)))
        (unless (and posn (eq (posn-area posn) 'mode-line))
          (error "elate: cannot hit the mode line of %s at column %d"
                 win col))
        posn)
    (let* ((pos (elate--mouse-resolve-pos win target))
           (posn (posn-at-point pos win)))
      (unless posn
        (error (concat "elate: position %d is not visible in window %s; "
                       "scroll it into view first, e.g. eval "
                       "(with-selected-window (get-buffer-window %S) "
                       "(goto-char %d) (recenter))")
               pos win (buffer-name (window-buffer win)) pos))
      posn)))

(defun elate--mouse-events (action button posn win to direction count)
  "The event sequence implementing ACTION at POSN, as a list.
BUTTON is the mouse button number (1-3).  For drag, TO describes the
destination in WIN (resolved via `elate--mouse-posn').  For wheel,
DIRECTION is \"up\" or \"down\" and COUNT the number of notches."
  (let* ((down (intern (format "down-mouse-%d" button)))
         (click (intern (format "mouse-%d" button)))
         ;; In special areas (mode line), a down event would enter the
         ;; binding's mouse-tracking loop (e.g. mouse-drag-mode-line),
         ;; which wedges without real motion events; the bare click
         ;; event is what mode-line bindings fire on.
         (special (posn-area posn)))
    (pcase action
      ("click"
       (if special
           (list (list click posn 1))
         (list (list down posn 1) (list click posn 1))))
      ("double"
       (let ((ddown (intern (format "double-down-mouse-%d" button)))
             (dclick (intern (format "double-mouse-%d" button))))
         (if special
             (list (list click posn 1) (list dclick posn 2))
           (list (list down posn 1) (list click posn 1)
                 (list ddown posn 2) (list dclick posn 2)))))
      ("drag"
       (let ((to-posn (elate--mouse-posn win to)))
         (list (list down posn 1)
               (list (intern (format "drag-mouse-%d" button))
                     posn to-posn))))
      ("wheel"
       (make-list count
                  (list (if (equal direction "up") 'wheel-up 'wheel-down)
                        posn)))
      (other (error "elate: unknown mouse action %S" other)))))

(defun elate--rpc-mouse (payload-b64)
  "Synthesize the mouse interaction described by PAYLOAD-B64 (base64 JSON).
Payload: {action, button, direction, count, delivery,
target: {buffer, pos, line, col, part}, to: {pos, line, col}}.
Delivery \"macro\" (default) dispatches synchronously via
`execute-kbd-macro'; \"events\" queues on `unread-command-events' (use
when the triggered command itself reads input)."
  (let* ((payload (json-parse-string (elate--decode-string payload-b64)
                                     :object-type 'alist
                                     :null-object nil :false-object nil))
         (action (alist-get 'action payload))
         (button (or (alist-get 'button payload) 1))
         (count (or (alist-get 'count payload) 1))
         (direction (or (alist-get 'direction payload) "down"))
         (delivery (or (alist-get 'delivery payload) "macro"))
         (target (alist-get 'target payload))
         (bufname (alist-get 'buffer target))
         (win (if bufname
                  (or (get-buffer-window bufname)
                      (error (concat "elate: buffer %S is not displayed in "
                                     "any window; show it first, e.g. eval "
                                     "(pop-to-buffer %S)")
                             bufname bufname))
                (selected-window)))
         (posn (elate--mouse-posn win target))
         (events (elate--mouse-events action button posn win
                                      (alist-get 'to payload)
                                      direction count)))
    (pcase delivery
      ("events"
       (setq unread-command-events
             (nconc unread-command-events (copy-sequence events))))
      ("macro" (execute-kbd-macro (vconcat events)))
      (other (error "elate: unknown mouse delivery %S" other)))
    (list :action action
          :button button
          :buffer (buffer-name (window-buffer win))
          :area (if (posn-area posn) (format "%s" (posn-area posn)) :null)
          :pos (elate--jnull (posn-point posn))
          :events (length events)
          :delivered delivery)))

(defun elate--rpc-echo ()
  "Echo area and active minibuffer only: the cheap targeted read.
A fraction of the cost of `state', which marshals every window's
visible text through the (slow) emacsclient print path."
  (list :echo (elate--jnull (current-message))
        :minibuffer (elate--minibuffer-info)))

(defun elate--rpc-idle ()
  "Idle/busy probe."
  (let ((idle (current-idle-time)))
    (list :idle (if idle (float-time idle) :null)
          :input-pending (elate--jbool (input-pending-p))
          :unread (length unread-command-events)
          :minibuffer-active (elate--jbool (active-minibuffer-window)))))

;;;; describe

(defun elate--definition-file (sym type)
  "File defining SYM (TYPE as for `find-lisp-object-file-name'), or nil."
  (condition-case nil
      (let ((file (find-lisp-object-file-name sym type)))
        (cond ((eq file 'C-source) "C source code")
              ((stringp file) file)))
    (error nil)))

(defun elate--doc-string (sym)
  "Docstring of function SYM, or nil."
  (ignore-errors (documentation sym)))

(defun elate--obsolete-info (sym prop)
  "Obsolescence plist for SYM read from PROP, or :null when current.
PROP is `byte-obsolete-info' (functions) or `byte-obsolete-variable'."
  (let ((obs (get sym prop)))
    (if (not obs)
        :null
      (list :use (if (car obs) (format "%s" (car obs)) :null)
            :since (let ((when- (car (last obs))))
                     (if when- (format "%s" when-) :null))))))

(defun elate--function-info (sym)
  "Structured description of function SYM (which may be undefined)."
  (let* ((defined (fboundp sym))
         (autoloaded (and defined (autoloadp (symbol-function sym))))
         ;; For a not-yet-loaded autoload, `help-function-arglist' returns
         ;; an explanatory *string*; report :null + :autoloaded t instead
         ;; of a quoted sentence masquerading as an arglist.
         (arglist (and defined
                       (condition-case nil
                           (help-function-arglist sym t)
                         (error t)))))         ; t = unknown
    (list :name (symbol-name sym)
          :defined (elate--jbool defined)
          :command (elate--jbool (commandp sym))
          :autoloaded (elate--jbool autoloaded)
          :arglist (if (and defined (listp arglist))
                       (format "%S" arglist)
                     :null)
          :obsolete (elate--obsolete-info sym 'byte-obsolete-info)
          :doc (elate--jnull (and defined (elate--doc-string sym)))
          :file (elate--jnull (and defined (elate--definition-file sym 'defun)))
          :keys (vconcat
                 (and defined
                      (ignore-errors
                        (mapcar #'key-description
                                (where-is-internal sym nil nil t))))))))

(defun elate--describe-key (keys)
  "Resolve KEYS (a kbd string) in the current context, like `describe-key'."
  ;; `kbd' is lenient: "C-x C-" parses without error and then merely looks
  ;; unbound, which teaches the caller "unbound" when they actually typo'd
  ;; the notation.  Reject the obvious malformation (a modifier with no
  ;; key) explicitly.
  (dolist (tok (split-string keys "[ \t]+" t))
    (when (string-match-p "\\`\\([ACHMSs]-\\)+\\'" tok)
      (error "elate: malformed key sequence %S: token %S has a modifier but no key"
             keys tok)))
  (with-current-buffer (elate--current-buffer)
    (let* ((vec (kbd keys))
           (binding (key-binding vec t)))
      (append
       (list :key keys
             :bound (elate--jbool binding)
             :prefix (elate--jbool (keymapp binding)))
       (cond
        ((null binding) (list :binding :null))
        ((keymapp binding) (list :binding "prefix keymap"))
        ((symbolp binding)
         (list :binding (symbol-name binding)
               :function (elate--function-info binding)))
        (t (list :binding (elate--clip-print binding))))))))

(defun elate--clip-print (obj)
  "OBJ printed, clipped to a sane length."
  (let ((print-length 50)
        (print-level 6))
    (let ((s (prin1-to-string obj)))
      (if (> (length s) 2000) (concat (substring s 0 2000) "...") s))))

(defun elate--describe-variable (sym)
  "Structured description of variable SYM in the current buffer's context."
  (with-current-buffer (elate--current-buffer)
    (let ((defined (boundp sym)))
      (list :name (symbol-name sym)
            :defined (elate--jbool defined)
            :value (if defined (elate--clip-print (symbol-value sym)) :null)
            :local (elate--jbool (and defined (local-variable-p sym)))
            :custom (elate--jbool (custom-variable-p sym))
            :obsolete (elate--obsolete-info sym 'byte-obsolete-variable)
            :doc (elate--jnull
                  (ignore-errors
                    (documentation-property sym 'variable-documentation)))
            :file (elate--jnull (elate--definition-file sym 'defvar))))))

(defun elate--describe-mode (sym)
  "Structured description of major/minor mode SYM.
:enabled is :null (unknown) for a minor mode whose state variable
cannot be resolved -- never a false \"disabled\"."
  (with-current-buffer (elate--current-buffer)
    (let ((minor (memq sym minor-mode-list)))
      (list :name (symbol-name sym)
            :defined (elate--jbool (fboundp sym))
            :minor (elate--jbool minor)
            :enabled (if (not minor)
                         (elate--jbool (eq major-mode sym))
                       ;; The state variable is usually the mode symbol but
                       ;; may differ (auto-fill-mode -> auto-fill-function).
                       (let ((var (elate--minor-mode-variable sym)))
                         (if (and var (boundp var))
                             (elate--jbool (symbol-value var))
                           :null)))
            :doc (elate--jnull (elate--doc-string sym))
            :file (elate--jnull (elate--definition-file sym 'defun))))))

(defun elate--rpc-describe (kind name)
  "Describe NAME as KIND: \"key\", \"function\", \"variable\", or \"mode\".
Key lookup resolves bindings in the current buffer's context, like
`describe-key'.  Unknown functions/variables/modes return
:defined false rather than an error."
  (pcase kind
    ("key" (elate--describe-key name))
    ((or "function" "variable" "mode")
     (let ((sym (intern-soft name)))
       (if (not sym)
           (list :name name :defined :false)
         (pcase kind
           ("function" (elate--function-info sym))
           ("variable" (elate--describe-variable sym))
           ("mode" (elate--describe-mode sym))))))
    (_ (error "elate: unknown describe kind %S (use key/function/variable/mode)"
              kind))))

;;;; ERT runner

(defconst elate--ert-max-backtrace 4000
  "Cap on a single test's rendered backtrace, in characters.
The whole result set travels through the ~50 KB/s emacsclient print
path; a suite with many failures must not blow the RPC timeout.")

(defconst elate--ert-max-messages 2000
  "Cap on a single test's captured *Messages* output, in characters.")

(defun elate--ert-parse-selector (str)
  "Parse STR into an ERT selector.
Reads STR as elisp: t, keywords (:failed, :new), compound selectors
\((tag NAME), (not ...), (and ...)) and quoted strings pass through.
A bare symbol is used as a test name when such a test exists, and as
a name-matching regexp otherwise (so \"my-pkg-\" works unquoted).
STR must be exactly one readable form: trailing tokens (a typo'd
selector would silently run a *different* set of tests) and reader
errors (an unbalanced \"(tag\") are loud errors, never a silent
fallback -- a regexp that doesn't read as one form must be quoted."
  (let ((str (string-trim str)))
    (if (string-empty-p str)
        t
      (pcase-let ((`(,form . ,end)
                   (condition-case err
                       (read-from-string str)
                     (error
                      (error "elate: unreadable ERT selector %S (%s); quote regexps, e.g. \"^my-pkg-\""
                             str (error-message-string err))))))
        (when (string-match-p "[^ \t\n]" (substring str end))
          (error "elate: ERT selector %S has trailing %S after the first form; quote regexps, e.g. \"^my-pkg-\""
                 str (string-trim (substring str end))))
        (cond ((eq form t) t)
              ((null form) str)
              ((keywordp form) form)
              ((symbolp form) (if (ert-test-boundp form) form str))
              (t form))))))

(defun elate--ert-backtrace-string (frames)
  "Render FRAMES (as stored in an ERT result) like an eval backtrace.
Frames at and below ERT's own test runner are dropped as machinery;
output is clipped at `elate--ert-max-backtrace'."
  (condition-case nil
      (when frames
        (let* ((cut (seq-position
                     frames nil
                     (lambda (fr _)
                       (memq (backtrace-frame-fun fr)
                             '(ert--run-test-internal ert-run-test)))))
               (frames (if cut (seq-take frames cut) frames))
               (print-length 50)
               (print-level 8))
          (elate--clip-string (backtrace-to-string frames)
                              elate--ert-max-backtrace)))
    (error nil)))

(defun elate--ert-status (result)
  "RESULT's status as a string.
ERT represents both `should' failures and signalled errors as
`ert-test-failed'; the condition distinguishes them, so \"failed\" vs
\"error\" is derived from its car."
  (cond ((ert-test-passed-p result) "passed")
        ((ert-test-skipped-p result) "skipped")
        ((ert-test-quit-p result) "quit")
        ((ert-test-aborted-with-non-local-exit-p result) "aborted")
        ((ert-test-failed-p result)
         (if (eq (car-safe (ert-test-result-with-condition-condition result))
                 'ert-test-failed)
             "failed"
           "error"))
        (t "unknown")))

(defun elate--ert-test-entry (test result duration)
  "Structured plist for one finished TEST with RESULT and DURATION."
  (let ((with-cond (ert-test-result-with-condition-p result)))
    (list :name (symbol-name (ert-test-name test))
          :status (elate--ert-status result)
          :expected (elate--jbool (ert-test-result-expected-p test result))
          :duration duration
          :condition (if with-cond
                         (let ((print-length 50)
                               (print-level 8))
                           (elate--clip-string
                            (prin1-to-string
                             (ert-test-result-with-condition-condition result))
                            2000))
                       :null)
          :backtrace (if with-cond
                         (elate--jnull
                          (elate--ert-backtrace-string
                           (ert-test-result-with-condition-backtrace result)))
                       :null)
          :messages (elate--clip-string
                     (or (ert-test-result-messages result) "")
                     elate--ert-max-messages))))

(defun elate--ert-contain-quit (orig info)
  "Run ORIG on INFO; record a quit inside the test body as its result.
Emacs 29 compatibility shim (installed as :around advice on
`ert--run-test-internal' only when `emacs-major-version' < 30).

Emacs 29's ERT catches in-test quits through the `debugger' variable
plus `debug-on-quit', but eval.c's signal_or_quit only consults the
debugger when no enclosing handler matches the signal -- and inside an
elate RPC, server.el's request machinery always provides one, so a
quit (keyboard-quit in a test body, or raw C-g hitting a running test)
sailed past ERT, past `elate-rpc' (whose handler matches `error', not
`quit'), and surfaced as an \"*ERROR*: Quit\" emacsclient reply that
killed the whole run.  Emacs >= 30's ERT catches the signal with
`handler-bind', which runs unconditionally; this advice gives 29 the
same contract: the quit becomes the test's result (status \"quit\",
counted unexpected) and the run continues.

Known, unavoidable difference from 30+: this `condition-case' handler
runs AFTER the stack has unwound, so the recorded result carries
:backtrace nil / :infos nil -- unlike 30's `handler-bind' capture,
which records a real backtrace and `ert-info' context for quits.
Status/expected-p/run-continuation (the actual contract) are
identical; only the per-test backtrace/condition payload is poorer."
  (condition-case err
      (funcall orig info)
    (quit
     (setf (ert--test-execution-info-result info)
           (make-ert-test-quit :condition err :backtrace nil :infos nil))
     nil)))

(when (< emacs-major-version 30)
  (advice-add 'ert--run-test-internal :around #'elate--ert-contain-quit))

(defun elate--rpc-load-file (path)
  "Load the elisp file at PATH into the session.
Used to bring a test file in before an ERT run.  A load error is
reported as a normal RPC error with backtrace."
  (let ((file (expand-file-name path)))
    (unless (file-readable-p file)
      (error "elate: cannot read file %s" file))
    (load file nil t)
    (list :loaded file)))

(defun elate--rpc-ert (selector-b64 &optional timeout)
  "Run ERT tests matching the selector decoded from SELECTOR-B64.
Tests run *interactively* (live redisplay, real window-system state)
inside this session via `ert-run-tests' with a structural listener:
per-test status, duration, captured *Messages* output, and -- for
failures -- condition and trimmed backtrace are read off the result
objects, never scraped from the *ert* buffer.  The INTERACTIVELY
argument of `ert-run-tests' is nil on purpose: its sole effect is a
`y-or-n-p' \"Abort testing?\" prompt after a test whose result is a
`quit' -- inside a synchronous RPC that prompt can never be answered
and would eat the whole timeout budget.  With nil, a quitting test
\(`keyboard-quit' in the body, or raw C-g hitting a running test) is
simply recorded with status \"quit\" and the run moves on.  TIMEOUT
\(seconds) arms a `with-timeout' around the whole run; like eval
timeouts it can only fire while the running test services timers
\(sleep-for, sit-for, accept-process-output) -- a hard elisp loop is
the controller's subprocess timeout's problem.  On timeout the reply
carries the partial results, :timed-out t, and the interrupted
test's name (it also appears in :tests with status \"aborted\")."
  (let* ((selector (elate--ert-parse-selector
                    (elate--decode-string selector-b64)))
         (entries nil)
         (interrupted nil)
         (t0 0.0)
         (run-start (float-time))
         (timed-out nil)
         (listener
          (lambda (event &rest args)
            (pcase event
              ('test-started
               (setq t0 (float-time)))
              ('test-ended
               ;; The with-timeout throw unwinds through ERT's
               ;; unwind-protect, which fires this event with an
               ;; "aborted" result for the interrupted test -- the
               ;; timeout forms themselves run only after the unwind,
               ;; so the interrupted test's name is captured here.
               (when (ert-test-aborted-with-non-local-exit-p (nth 2 args))
                 (setq interrupted (ert-test-name (nth 1 args))))
               (push (elate--ert-test-entry (nth 1 args) (nth 2 args)
                                            (- (float-time) t0))
                     entries))))))
    (unwind-protect
        (catch 'elate--ert-timeout
          (if (and (numberp timeout) (> timeout 0))
              (with-timeout (timeout
                             (setq timed-out t)
                             (throw 'elate--ert-timeout nil))
                (ert-run-tests selector listener nil))
            (ert-run-tests selector listener nil)))
      ;; ERT entered its own debugger for every failing test; re-arm so
      ;; later RPC/eval errors still capture backtraces.
      (elate--rearm-debugger))
    (let* ((tests (nreverse entries))
           (count (lambda (status)
                    (seq-count (lambda (e)
                                 (equal (plist-get e :status) status))
                               tests))))
      (list :selector (let ((print-length 50) (print-level 8))
                        (prin1-to-string selector))
            :total (length tests)
            :passed (funcall count "passed")
            :failed (funcall count "failed")
            :errors (funcall count "error")
            :skipped (funcall count "skipped")
            :unexpected (seq-count (lambda (e)
                                     (eq (plist-get e :expected) :false))
                                   tests)
            :duration (- (float-time) run-start)
            :timed-out (elate--jbool timed-out)
            :interrupted (if (and timed-out interrupted)
                             (symbol-name interrupted)
                           :null)
            :error (if timed-out
                       (format "elate: ERT run timed out after %gs%s"
                               timeout
                               (if interrupted
                                   (format " while running %s" interrupted)
                                 ""))
                     :null)
            :tests (vconcat tests)))))

;;;; Lint: byte-compile + checkdoc

(defun elate--lint-byte-compile (file)
  "Byte-compile FILE; return warning/error item plists.
The .elc goes to <session>/lint/ -- never next to the source, where a
stale .elc on `load-path' would shadow newer sources in later loads --
and is deleted right afterwards; only the log matters.  Items are
parsed from a fresh *Compile-Log*, whose \"file:line:col: Severity:\"
lines are the byte compiler's stable programmatic surface.  All
cleanup (kill the log buffer, delete the .elc) runs in the
`unwind-protect', so an interrupted lint (quit, `with-timeout' firing
mid-compile) leaves no residue in the session."
  (require 'bytecomp)
  (let* ((dest-dir (expand-file-name "lint" elate-session-dir))
         (dest (expand-file-name
                (concat (file-name-nondirectory file) "c") dest-dir))
         (byte-compile-dest-file-function (lambda (_f) dest))
         (byte-compile-verbose nil)
         (log-buf (get-buffer-create byte-compile-log-buffer))
         (items nil))
    (make-directory dest-dir t)
    (unwind-protect
        (progn
          (with-current-buffer log-buf
            (let ((inhibit-read-only t)) (erase-buffer)))
          (let ((inhibit-message t))
            (condition-case err
                (byte-compile-file file)
              (error
               (push (list :file file :tool "byte-compile"
                           :line :null :col :null :severity "error"
                           :message (error-message-string err))
                     items))))
          (with-current-buffer log-buf
            (goto-char (point-min))
            (while (not (eobp))
              (let ((line (buffer-substring-no-properties
                           (line-beginning-position) (line-end-position))))
                (cond
                 ;; :file is always the absolute input path: the log prints
                 ;; whatever name the compiler used (often the bare basename),
                 ;; and a single-file lint owns every entry anyway.
                 ((string-match (concat "\\`\\(.+?\\):\\([0-9]+\\):\\([0-9]+\\):"
                                        " \\(Warning\\|Error\\): \\(.*\\)\\'")
                                line)
                  (push (list :file file
                              :tool "byte-compile"
                              :line (string-to-number (match-string 2 line))
                              :col (string-to-number (match-string 3 line))
                              :severity (downcase (match-string 4 line))
                              :message (match-string 5 line))
                        items))
                 ((string-match "\\`\\(.+?\\): ?\\(Warning\\|Error\\): \\(.*\\)\\'"
                                line)
                  (push (list :file file
                              :tool "byte-compile"
                              :line :null :col :null
                              :severity (downcase (match-string 2 line))
                              :message (match-string 3 line))
                        items))))
              (forward-line 1))))
      (when (buffer-live-p log-buf) (kill-buffer log-buf))
      (when (file-exists-p dest) (delete-file dest)))
    (nreverse items)))

(defun elate--lint-checkdoc (file)
  "Run checkdoc over FILE programmatically; return item plists.
Items are collected via `checkdoc-create-error-function' (positions
resolved to line/column in the visited buffer).  A buffer we had to
create for the visit is killed afterwards so the session's buffer
list stays clean."
  (require 'checkdoc)
  (let* ((items nil)
         (existing (find-buffer-visiting file))
         (checkdoc-autofix-flag 'never)
         (checkdoc-diagnostic-buffer " *elate-checkdoc*")
         (checkdoc-create-error-function
          (lambda (text start _end &optional _unfixable)
            (push (list :file file :tool "checkdoc"
                        :line (if start (line-number-at-pos start t) :null)
                        :col (if start
                                 (save-excursion (goto-char start)
                                                 (current-column))
                               :null)
                        :severity "warning"
                        :message text)
                  items)
            nil)))
    (unwind-protect
        (let ((buf (or existing (find-file-noselect file))))
          (unwind-protect
              (with-current-buffer buf
                (let ((inhibit-message t))
                  (checkdoc-current-buffer t)))
            (unless existing (kill-buffer buf))))
      (when (get-buffer checkdoc-diagnostic-buffer)
        (kill-buffer checkdoc-diagnostic-buffer)))
    (nreverse items)))

(defun elate--lint-file (file)
  "Byte-compile + checkdoc FILE; return the combined item list."
  (append (elate--lint-byte-compile file)
          (condition-case err
              (elate--lint-checkdoc file)
            (error
             (list (list :file file :tool "checkdoc"
                         :line :null :col :null
                         :severity "error"
                         :message (error-message-string err)))))))

(defun elate--rpc-lint (path &optional timeout)
  "Lint the file at PATH: byte-compile + checkdoc, as structured items.
Each item is {file, tool, line, col, severity, message}.  PATH is a
path on purpose: file contents must never travel over the (~1 MiB)
argv transport.  NOTE: byte-compilation runs in THIS session, so the
file's compile-time code (`eval-when-compile', macro expansion,
top-level `require's) is EXECUTED here -- inherent to in-session
linting against the session's load-path.  TIMEOUT (seconds) arms a
`with-timeout' backstop around the lint, same discipline as eval/ert:
it can only fire while the compile-time code services timers; a hard
elisp loop falls to the controller's subprocess timeout.  Cleanup
\(log buffer, .elc, visit buffers) runs in `unwind-protect's, so a
timed-out or quit-interrupted lint leaves no residue."
  (let ((file (expand-file-name path)))
    (unless (file-readable-p file)
      (error "elate: cannot read file %s" file))
    (list :file file
          :items (vconcat
                  (if (and (numberp timeout) (> timeout 0))
                      (with-timeout (timeout
                                     (error "elate: lint of %s timed out after %gs (likely stuck compile-time code; lint untrusted files in a throwaway session)"
                                            file timeout))
                        (elate--lint-file file))
                    (elate--lint-file file))))))

;;;; Profiler

(defconst elate--profiler-max-functions 30
  "Cap on the number of entries in a profile report's top-function list.")

(defconst elate--profiler-max-tree-nodes 400
  "Total node budget for one rendered profile calltree.
The whole report travels through the ~50 KB/s emacsclient print path;
a deep, bushy calltree must not blow the RPC timeout.")

(defconst elate--profiler-default-depth 6
  "Default depth limit for the rendered profile calltree.")

(defconst elate--profiler-max-depth 20
  "Hard cap on the rendered calltree depth.
`json-serialize' (Emacs 29-31) refuses nesting deeper than ~50
levels, and every calltree level costs two (the node object plus its
children array) on top of the ~4-level reply envelope: depth 20 peaks
at ~44 nested levels, comfortably inside the limit, whereas depth 24+
made `elate--encode' fail on deep-recursion profiles -- exactly the
trees a high depth is wanted for.")

(defun elate--profiler-entry-name (entry)
  "Human-readable name for profiler backtrace/calltree ENTRY.
Mirrors the profiler-report UI's naming: t is the \"Others\" bucket,
`...' is the unified tree's leftover node, symbols print as-is, and
anonymous closures/byte-code objects go through
`help-fns-function-name'."
  (cond ((eq entry t) "Others")
        ((eq entry '\.\.\.) "...")
        ((symbolp entry) (symbol-name entry))
        ((stringp entry) entry)
        (t (or (ignore-errors
                 (substring-no-properties (help-fns-function-name entry)))
               (elate--clip-print entry)))))

(defun elate--profiler-merge-log (old new)
  "Merge profiler log NEW into OLD (hash tables); return the merged log."
  (cond ((null old) new)
        ((null new) old)
        (t (maphash (lambda (k v) (puthash k (+ v (gethash k old 0)) old)) new)
           old)))

(defun elate--profiler-drain ()
  "Move pending live profiler samples into profiler.el's log variables.
Unlike stock `profiler-report' (which replaces the variables, silently
discarding samples retrieved by an earlier report), live samples are
MERGED in, so repeated reports while profiling keep accumulating.

Emacs 29 quirk (profiler.c): `profiler-cpu-log'/`profiler-memory-log'
allocate the replacement log -- a pre-filled Lisp hash table of
profiler-log-size key vectors, ~2-3 MB -- BEFORE detaching the old
one, so reading a log while the *memory* profiler is running records
those megabytes as self-samples (attributed to profiler-memory-log)
into the very log being returned.  Emacs >= 30 stops the profiler
around the export in C (and its replacement log is raw C memory,
invisible to malloc_probe).  Mimic that here: on 29, pause the memory
profiler across the reads.  No samples are lost -- the reads drain
everything collected so far, and sampling resumes right after."
  (let ((pause (and (< emacs-major-version 30)
                    (profiler-memory-running-p))))
    (when pause (profiler-memory-stop))
    ;; unwind-protect: a signal/quit between the stop above and the
    ;; restart (merge error, memory-full, C-g hitting the RPC) must not
    ;; leave the memory profiler silently stopped for the rest of the
    ;; profile window.
    (unwind-protect
        (progn
          (when (and (fboundp 'profiler-cpu-running-p)
                     (profiler-cpu-running-p))
            (setq profiler-cpu-log
                  (elate--profiler-merge-log profiler-cpu-log
                                             (profiler-cpu-log))))
          (when (or pause (profiler-memory-running-p))
            (setq profiler-memory-log
                  (elate--profiler-merge-log profiler-memory-log
                                             (profiler-memory-log)))))
      (when pause (profiler-memory-start)))))

(defun elate--profiler-log-total (log)
  "Sum of all sample counts in LOG."
  (let ((n 0))
    (when log
      (maphash (lambda (_bt count) (setq n (+ n count))) log))
    n))

(defun elate--profiler-percent (count total)
  "COUNT as a percentage of TOTAL, rounded to one decimal."
  (/ (round (* 1000.0 count) (max 1 total)) 10.0))

(defun elate--profiler-top-functions (log total)
  "Aggregate LOG per function; return (:functions VEC :functions-truncated B).
Each entry carries :self (samples with the function on top of the
stack), :total (samples with it anywhere in the backtrace, counted
once per backtrace), and both as percentages of TOTAL.  Sorted by
self count, capped at `elate--profiler-max-functions'."
  (let ((self (make-hash-table :test 'equal))
        (agg (make-hash-table :test 'equal)))
    (maphash
     (lambda (backtrace count)
       (let ((seen (make-hash-table :test 'equal))
             (max (length backtrace)))
         (let ((top (and (> max 0) (aref backtrace 0))))
           (when top
             (let ((name (elate--profiler-entry-name top)))
               (puthash name (+ count (gethash name self 0)) self))))
         (dotimes (i max)
           (let ((f (aref backtrace i)))
             (when f
               (let ((name (elate--profiler-entry-name f)))
                 (unless (gethash name seen)
                   (puthash name t seen)
                   (puthash name (+ count (gethash name agg 0)) agg))))))))
     log)
    (let ((entries nil))
      (maphash
       (lambda (name total-count)
         (let ((self-count (gethash name self 0)))
           (push (list :name name
                       :self self-count
                       :self-percent (elate--profiler-percent self-count total)
                       :total total-count
                       :total-percent (elate--profiler-percent total-count
                                                               total))
                 entries)))
       agg)
      (setq entries (sort entries
                          (lambda (a b)
                            (or (> (plist-get a :self) (plist-get b :self))
                                (and (= (plist-get a :self) (plist-get b :self))
                                     (> (plist-get a :total)
                                        (plist-get b :total)))))))
      (list :functions (vconcat (seq-take entries
                                          elate--profiler-max-functions))
            :functions-truncated
            (elate--jbool (> (length entries)
                             elate--profiler-max-functions))))))

(defun elate--profiler-tree-children (node depth max-depth total budget)
  "Render NODE's children (sorted by count) as a vector of node plists.
DEPTH is the current depth, MAX-DEPTH the limit, TOTAL the profile's
sample total (for percentages), and BUDGET a one-element list holding
the remaining node allowance (mutated).  Children beyond the depth
limit or the budget are dropped; the parent carries
:children-truncated t when that happens."
  (let ((children (sort (copy-sequence (profiler-calltree-children node))
                        #'profiler-calltree-count>))
        (out nil)
        (truncated nil))
    (if (>= depth max-depth)
        (setq truncated (consp children))
      (dolist (child children)
        (if (<= (car budget) 0)
            (setq truncated t)
          (setcar budget (1- (car budget)))
          (let ((sub (elate--profiler-tree-children
                      child (1+ depth) max-depth total budget)))
            (push (list :name (elate--profiler-entry-name
                               (profiler-calltree-entry child))
                        :count (profiler-calltree-count child)
                        :percent (elate--profiler-percent
                                  (profiler-calltree-count child) total)
                        :children (car sub)
                        :children-truncated (cdr sub))
                  out)))))
    (cons (vconcat (nreverse out)) (elate--jbool truncated))))

(defun elate--profiler-section (log units max-depth)
  "Structured report for one profiler LOG: totals, top functions, calltree.
UNITS labels what the counts mean (\"samples\" for cpu, \"bytes\" for
mem).  The calltree is profiler.el's own unified tree
\(`profiler-calltree-build'), depth-limited to MAX-DEPTH and capped at
`elate--profiler-max-tree-nodes' nodes."
  (let* ((total (elate--profiler-log-total log))
         (tree (profiler-calltree-build log))
         (budget (list elate--profiler-max-tree-nodes)))
    (pcase-let ((`(,children . ,truncated)
                 (elate--profiler-tree-children tree 0 max-depth total budget)))
      (append
       (list :units units
             :total total
             :depth max-depth
             :tree children
             :tree-truncated (if (or (eq truncated t) (<= (car budget) 0))
                                 t
                               :false))
       (elate--profiler-top-functions log total)))))

(defun elate--profiler-report (depth)
  "Structured report over the accumulated profiler logs.
Sections :cpu and/or :mem appear for the data that exists; an error is
signalled when there is none.  DEPTH limits the rendered calltree."
  (elate--profiler-drain)
  (unless (or profiler-cpu-log profiler-memory-log)
    (error "elate: no profiler data; run `profile start' (or one-shot `profile run') first"))
  (let ((depth (if (and (numberp depth) (> depth 0))
                   (min (floor depth) elate--profiler-max-depth)
                 elate--profiler-default-depth)))
    (append
     (list :running (elate--jbool (profiler-running-p)))
     (and profiler-cpu-log
          (list :cpu (elate--profiler-section profiler-cpu-log
                                              "samples" depth)))
     (and profiler-memory-log
          (list :mem (elate--profiler-section profiler-memory-log
                                              "bytes" depth))))))

(defun elate--rpc-profiler (action &optional mode depth)
  "Drive Emacs's native profiler: ACTION is start/stop/report.
For \"start\", MODE is \"cpu\" (default), \"mem\", or \"cpu+mem\";
starting resets any previously collected logs and garbage-collects
first (so garbage predating the window is never charged to it); a
profile covers exactly one start..stop window.  \"stop\" drains the
pending samples into profiler.el's log variables and stops the
samplers.  \"report\"
\(optionally depth-limited to DEPTH) renders the accumulated logs --
it works both while profiling and after \"stop\".  NOTE: profiles are
session-history dependent (everything the session ran is in the
samples, including elate's own RPC servicing); profile in a fresh
session for authoritative numbers."
  (pcase action
    ("start"
     (let ((mode (intern (or mode "cpu"))))
       (unless (memq mode '(cpu mem cpu+mem))
         (error "elate: unknown profiler mode %S (use cpu/mem/cpu+mem)" mode))
       (when (profiler-running-p)
         (error "elate: the profiler is already running; `profile stop' (or `profile report') it first"))
       ;; Capability check BEFORE the reset: on a build without the
       ;; SIGPROF profiler a failed cpu start must not destroy the
       ;; previous profile's logs.
       (when (and (memq mode '(cpu cpu+mem))
                  (not (fboundp 'profiler-cpu-start)))
         (error "elate: this Emacs lacks the SIGPROF cpu profiler; use --mem"))
       ;; Emacs 29: a stopped profiler's C-side log persists and is
       ;; REUSED by the next start (profiler.c only allocates when the
       ;; log is nil), so samples recorded between the last drain and
       ;; the stop would leak into the new window.  Reading the log
       ;; discards it (29 nils the variable when not running).  Do NOT
       ;; do this on 30.x: its export_log frees the C log and lacks
       ;; 31's NULL guard, so a second read while stopped segfaults.
       ;; The ignore-errors absorbs 29's `profiler-cpu-log' puthash on
       ;; nil when there was no cpu log at all.
       ;; Deliberate asymmetry: on 30.x, samples recorded between the
       ;; drain's read and `profiler-*-stop' stay in the C log, and
       ;; `profiler-*-start' reuses a non-NULL log there -- a sub-ms
       ;; stale-sample window that can leak into the next profile.
       ;; 29 discards; 30 can't (the segfault above), and the window
       ;; is invisible to the empty-window assertion.
       (when (< emacs-major-version 30)
         (when (and (fboundp 'profiler-cpu-log)
                    (not (profiler-running-p 'cpu)))
           (ignore-errors (profiler-cpu-log)))
         (unless (profiler-memory-running-p)
           (ignore-errors (profiler-memory-log))))
       (setq profiler-cpu-log nil
             profiler-memory-log nil)
       ;; Flush garbage that predates this profile window.  At the end
       ;; of every GC, alloc.c (29-31 alike) reports the bytes that GC
       ;; freed to a RUNNING memory profiler as one malloc_probe sample
       ;; attributed to whatever code happened to trigger the GC.  The
       ;; logs discarded just above are ~1.8 MB of live hash/vector
       ;; objects per log on Emacs 29 (where logs are Lisp data), so
       ;; without this sweep the new window's first GC would charge the
       ;; PREVIOUS profile's freed logs -- plus any other pre-window
       ;; garbage -- to the fresh window (observed: an empty start..stop
       ;; window "allocating" 1.79 MB on 29.4, exactly one C-side log).
       ;; With the profilers still off, this GC itself records nothing.
       (garbage-collect)
       (when (memq mode '(cpu cpu+mem))
         (profiler-cpu-start profiler-sampling-interval))
       (when (memq mode '(mem cpu+mem))
         (profiler-memory-start))
       (list :started (symbol-name mode)
             :sampling-interval (if (memq mode '(cpu cpu+mem))
                                    profiler-sampling-interval
                                  :null))))
    ("stop"
     (let ((cpu (profiler-running-p 'cpu))
           (mem (profiler-running-p 'mem)))
       (elate--profiler-drain)
       (when cpu (profiler-cpu-stop))
       (when mem (profiler-memory-stop))
       (list :stopped (elate--jbool (or cpu mem))
             :cpu (elate--jbool cpu)
             :mem (elate--jbool mem)
             :cpu-samples (elate--profiler-log-total profiler-cpu-log)
             :mem-bytes (elate--profiler-log-total profiler-memory-log))))
    ("report" (elate--profiler-report depth))
    (other (error "elate: unknown profiler action %S (use start/stop/report)"
                  other))))

;;;; Benchmark

(defun elate--memory-delta (before after)
  "Plist of `memory-use-counts' deltas, AFTER minus BEFORE."
  (let ((keys '(:conses :floats :vector-cells :symbols
                :string-chars :intervals :strings))
        (out nil))
    (while keys
      (setq out (nconc out (list (car keys) (- (car after) (car before))))
            keys (cdr keys)
            before (cdr before)
            after (cdr after)))
    out))

(defun elate--rpc-bench (form-b64 &optional repetitions timeout)
  "Benchmark the elisp source decoded from FORM-B64.
The `benchmark-run-compiled' mechanism: the form is wrapped in a
lambda and byte-compiled, then timed over REPETITIONS calls via
`benchmark-call'; if byte-compilation fails the interpreted closure is
benchmarked instead (:compiled nil, :compile-error says why).  The
result carries elapsed/mean seconds, GC runs and GC seconds during the
run, `memory-use-counts' deltas, and `gcs-done'/`gc-elapsed' deltas as
allocation context.  Errors signalled by the form are reported with a
backtrace, like eval.  TIMEOUT arms a `with-timeout' around the run;
like eval it can only fire at a timer-servicing point -- a hard elisp
loop falls to the controller's subprocess timeout.  NOTE: results are
session-history dependent (loaded code, GC state); benchmark in a
fresh session for authoritative numbers."
  (require 'benchmark)
  (let* ((src (elate--decode-string form-b64))
         (form (read (concat "(progn\n" src "\n)")))
         (reps (if (and (numberp repetitions) (>= repetitions 1))
                   (floor repetitions)
                 1))
         (fn (eval (list 'lambda nil form) t))
         (compiled nil)
         (compile-error nil))
    ;; Compiled path first; interpreted fallback when compilation fails.
    ;; The compile log goes to a private buffer (killed below) so a
    ;; warning-producing form leaves no *Compile-Log* in the session.
    (let ((byte-compile-log-buffer " *elate-bench-log*")
          (byte-compile-verbose nil)
          (inhibit-message t))
      (unwind-protect
          (condition-case cerr
              (let ((bc (byte-compile fn)))
                (if (functionp bc)
                    (setq fn bc
                          compiled t)
                  (setq compile-error "byte-compile returned no function")))
            (error (setq compile-error (error-message-string cerr))))
        (when (get-buffer byte-compile-log-buffer)
          (kill-buffer byte-compile-log-buffer))))
    (let ((mem0 (memory-use-counts))
          (gcs0 gcs-done)
          (gc-el0 gc-elapsed)
          (result nil)
          (errstr nil)
          (bt nil))
      (letrec ((capture
                (lambda (&rest _args)
                  (elate--rearm-debugger)
                  (unless bt
                    (setq bt (elate--backtrace-string
                              capture
                              (lambda (fr)
                                ;; benchmark-call and everything outward
                                ;; (with-timeout plumbing, the RPC) is
                                ;; machinery; the user's form frames are
                                ;; inside it.
                                (memq (backtrace-frame-fun fr)
                                      '(benchmark-call elate--rpc-bench)))))))))
        (let ((debugger capture)
              (debug-on-error t))
          (condition-case err
              (setq result
                    (if (and (numberp timeout) (> timeout 0))
                        (with-timeout (timeout
                                       (error "elate: bench timed out after %gs"
                                              timeout))
                          (benchmark-call fn reps))
                      (benchmark-call fn reps)))
            ((debug error) (setq errstr (error-message-string err))))))
      (append
       (list :repetitions reps
             :compiled (elate--jbool compiled)
             :compile-error (elate--jnull compile-error)
             :error (elate--jnull errstr)
             :backtrace (elate--jnull bt))
       (when result
         (pcase-let ((`(,elapsed ,gc-runs ,gc-time) result))
           (list :elapsed elapsed
                 :mean (/ elapsed (float reps))
                 :gc-runs gc-runs
                 :gc-elapsed gc-time)))
       (list :gcs-done-delta (- gcs-done gcs0)
             :gc-elapsed-delta (- gc-elapsed gc-el0)
             :memory-deltas (elate--memory-delta mem0 (memory-use-counts)))))))

;;;; clean-install

(defun elate--write-clean-install-file ()
  "Persist `elate--clean-installed' to <session>/clean-install.json.
File-based (rather than RPC-only) so `elate info' can report the
install results even for a dead session."
  (when elate-session-dir
    (let ((coding-system-for-write 'utf-8)
          (inhibit-message t))
      (write-region
       (json-serialize
        (elate--clean
         (list :installed (vconcat (reverse elate--clean-installed)))))
       nil (expand-file-name "clean-install.json" elate-session-dir)
       nil 'silent))))

(defun elate--package-desc-for (path)
  "Package description for PATH: an .el file, a package tar, or a directory.
The same three input kinds `package-install-file' accepts."
  (with-temp-buffer
    (cond
     ((file-directory-p path)
      ;; Mirror package-install-file's own directory handling:
      ;; package-dir-info asserts dired-mode and reads default-directory.
      (setq default-directory (file-name-as-directory path))
      (dired-mode)
      (package-dir-info))
     ((string-match-p "\\.tar\\'" path)
      (insert-file-contents-literally path)
      (tar-mode)
      (package-tar-file-info))
     (t
      (insert-file-contents path)
      (package-buffer-info)))))

(defun elate--missing-deps (desc)
  "DESC's dependencies that cannot be satisfied offline, as strings.
A dependency is satisfiable when it is built in (minimum version
honored), already installed in this session, or an `emacs' requirement
this Emacs meets."
  (let (missing)
    (dolist (req (package-desc-reqs desc))
      (let ((name (car req))
            (ver (cadr req)))
        (cond
         ((eq name 'emacs)
          (unless (version-list-<= ver (version-to-list emacs-version))
            (push (format "emacs %s (this is %s)"
                          (package-version-join ver) emacs-version)
                  missing)))
         ((package-installed-p name ver))
         (t (push (format "%s (%s)" name (package-version-join ver))
                  missing)))))
    (nreverse missing)))

(defconst elate--lexical-cookie-warning-re
  "file has no .lexical-binding. directive on its first line"
  "The byte compiler's missing-cookie warning, quote-style agnostic.
The message text passes through `text-quoting-style' rendering, so the
grave quotes in bytecomp.el's source may arrive as curly ones.")

(defun elate--prop-line-no-byte-compile-p (line)
  "Non-nil when LINE's -*- ... -*- section sets `no-byte-compile' to t.
LINE is a file's first line; only the file-local-variable prop line is
consulted (a mere mention of \"no-byte-compile: t\" in a comment or
docstring elsewhere must not count)."
  (when (string-match "-\\*-\\(.*?\\)-\\*-" line)
    (string-match-p
     "\\(?:\\`\\|;\\)[ \t]*no-byte-compile:[ \t]*t[ \t]*\\(?:;\\|\\'\\)"
     (match-string 1 line))))

(defun elate--noncompiled-cookieless-files (dir)
  "Basenames of .el files in DIR that are `no-byte-compile' and cookie-less.
These are the files for which Emacs 30.x emits a spurious
missing-lexical-binding warning: bytecomp.el warns BEFORE checking
`no-byte-compile' (Emacs 31 reordered the checks; Emacs 29 has no such
warning), so `package--compile's byte-recompile-directory pass warns
about generated files it then refuses to compile -- notably the
NAME-pkg.el that package.el itself writes, whose first line carries
only \"-*- no-byte-compile: t -*-\".  That prop line is the only
legitimate source, so only the first-line -*- ... -*- section is
parsed (a file merely mentioning no-byte-compile elsewhere is not
exempt from compilation and must not fund the filter's budget)."
  (when (and dir (file-directory-p dir))
    (let (out)
      (dolist (file (directory-files dir t "\\.el\\'"))
        (with-temp-buffer
          (insert-file-contents file nil 0 4096)
          (let ((first (buffer-substring (point-min) (line-end-position))))
            (when (and (not (string-match-p "lexical-binding" first))
                       (elate--prop-line-no-byte-compile-p first))
              (push (file-name-nondirectory file) out)))))
      out)))

(defun elate--drop-spurious-lexical-warnings (warnings dir)
  "WARNINGS without the bogus missing-cookie entries for DIR's generated files.
For every never-compiled cookie-less file in DIR (see
`elate--noncompiled-cookieless-files') at most ONE matching warning is
removed; a warning that names a different file (the post-init
Compile-Log shape carries \"file.el:LINE:COL:\") or exceeds that
budget -- i.e. a real missing-cookie warning about the package's own
code -- is kept.  Init-time installs collect warnings from
`delayed-warnings-list', which has no file context; the budget keeps
the removal honest there.

The budget is funded ONLY on Emacs 30.x: the warn-before-checking-
`no-byte-compile' ordering exists only there.  Emacs 29's bytecomp has
no missing-cookie warning at all, and Emacs 31+ checks
`no-byte-compile' first, so neither emits the spurious warning -- on
those majors a matching warning is always genuine (the package's own
cookie-less code) and funding the budget from files on disk would eat
it (REVIEW-CI1 finding 1, verified live on 31.0.90)."
  (let* ((spurious (and (= emacs-major-version 30)
                        (elate--noncompiled-cookieless-files dir)))
         (budget (length spurious)))
    (if (zerop budget)
        warnings
      (seq-remove
       (lambda (w)
         (and (> budget 0)
              (string-match-p elate--lexical-cookie-warning-re w)
              (or (not (string-match "\\`\\([^ :]+\\.el\\):" w))
                  (member (file-name-nondirectory (match-string 1 w))
                          spurious))
              (setq budget (1- budget))))
       warnings))))

(defun elate-clean-install (path)
  "Install the package at PATH for real, into the sandbox `package-user-dir'.
PATH is an .el file, a package tar, or a package directory; the
install goes through `package-install-file', so autoload generation,
Package-Requires handling, and byte-compilation of the installed copy
are exercised exactly as for a user install.  The sandbox has no
network and `package-archives' is nil, so dependencies that are not
built in (or already installed here) cannot be fetched: they are
detected up front and signalled as one clear error naming every
missing dependency.  Byte-compile warnings produced during the install
are collected per package and written (with name/version/install dir)
to <session>/clean-install.json for `elate info'."
  (require 'package)
  (require 'bytecomp)
  (let* ((path (expand-file-name path))
         (desc (elate--package-desc-for path))
         (name (package-desc-name desc))
         (missing (elate--missing-deps desc)))
    (when missing
      (error "elate: cannot clean-install %s: missing dependencies (the sandbox has no network, package-archives is nil): %s"
             name (string-join missing ", ")))
    (let ((log-buf (get-buffer-create byte-compile-log-buffer))
          (delayed-before (bound-and-true-p delayed-warnings-list))
          (warnings nil))
      (with-current-buffer log-buf
        (let ((inhibit-read-only t)) (erase-buffer)))
      (unwind-protect
          (let ((inhibit-message t))
            (package-install-file path)
            ;; Post-init installs: warnings land in the compile log.
            (with-current-buffer log-buf
              (goto-char (point-min))
              (while (not (eobp))
                (let ((line (buffer-substring-no-properties
                             (line-beginning-position) (line-end-position))))
                  (when (string-match-p "\\(Warning\\|Error\\): " line)
                    (push line warnings)))
                (forward-line 1)))
            ;; Init-time installs (the clean-install config mode runs
            ;; from init.el): `display-warning' DEFERS warnings to
            ;; `delayed-warnings-list' until after init, so the log
            ;; buffer is still empty here -- collect the delta instead.
            (let ((delayed (bound-and-true-p delayed-warnings-list)))
              (while (and delayed (not (eq delayed delayed-before)))
                (let ((entry (car delayed)))  ; (TYPE MESSAGE LEVEL BUF)
                  (push (format "%s: %s" (nth 0 entry) (nth 1 entry))
                        warnings))
                (setq delayed (cdr delayed)))))
        (when (buffer-live-p log-buf) (kill-buffer log-buf)))
      (let* ((installed (cadr (assq name package-alist)))
             (entry (list :name (symbol-name name)
                          :version (package-version-join
                                    (package-desc-version desc))
                          :dir (elate--jnull
                                (and installed (package-desc-dir installed)))
                          :warnings (vconcat
                                     (elate--drop-spurious-lexical-warnings
                                      (nreverse warnings)
                                      (and installed
                                           (package-desc-dir installed)))))))
        (push entry elate--clean-installed)
        (elate--write-clean-install-file)
        entry))))

;;;; Faces / text properties / overlays

(defconst elate--max-prop-runs 1000
  "Cap on the number of property runs in one buffer-props dump.")

(defconst elate--max-run-text 200
  "Cap on the text excerpt carried by a single property run.")

(defconst elate--max-overlays 200
  "Cap on the number of overlays in one dump.")

(defun elate--face-list (face)
  "FACE (a face spec value) normalized to a vector of printable strings.
Handles named faces, lists of faces, anonymous plist faces, and the
legacy (foreground-color . COLOR) cons; nil stays nil."
  (cond ((null face) nil)
        ((symbolp face) (vector (symbol-name face)))
        ((and (consp face) (keywordp (car face)))   ; anonymous plist face
         (vector (elate--clip-print face)))
        ((and (consp face) (proper-list-p face))    ; list of faces
         (vconcat (mapcar (lambda (f)
                            (if (symbolp f)
                                (symbol-name f)
                              (elate--clip-print f)))
                          face)))
        (t (vector (elate--clip-print face)))))

(defun elate--props-signature (pos)
  "Snapshot of the interesting text properties at POS in the current buffer.
Returns (FACE DISPLAY INVISIBLE BUTTON FIELD KEYMAP-P)."
  (list (get-text-property pos 'face)
        (get-text-property pos 'display)
        (get-text-property pos 'invisible)
        (get-text-property pos 'button)
        (get-text-property pos 'field)
        (and (or (get-text-property pos 'keymap)
                 (get-text-property pos 'local-map))
             t)))

(defun elate--prop-run-entry (beg end sig)
  "Plist for the property run BEG..END whose signature is SIG."
  (pcase-let ((`(,face ,display ,invisible ,button ,field ,keymap) sig))
    (let* ((text (buffer-substring-no-properties beg end))
           (truncated (> (length text) elate--max-run-text)))
      (append
       (list :start beg :end end
             :line (line-number-at-pos beg t)
             :text (if truncated (substring text 0 elate--max-run-text) text)
             :text-truncated (elate--jbool truncated))
       (and face (list :face (elate--face-list face)))
       (and display (list :display (elate--clip-print display)))
       (and invisible (list :invisible (elate--clip-print invisible)))
       (and button (list :button t))
       (and field (list :field (elate--clip-print field)))
       (and keymap (list :keymap t))))))

(defun elate--prop-runs (beg end)
  "Run-length-encoded interesting text properties of BEG..END.
Contiguous spans with an identical property signature become one run;
boundaries caused by uninteresting properties (fontified, ...) are
merged away.  Returns {:runs VECTOR :truncated BOOL}."
  (let ((runs nil)
        (n 0)
        (truncated nil)
        (pos beg))
    (while (< pos end)
      (if (>= n elate--max-prop-runs)
          (setq truncated t
                pos end)
        (let ((sig (elate--props-signature pos))
              (next (next-property-change pos nil end)))
          (while (and (< next end)
                      (equal sig (elate--props-signature next)))
            (setq next (next-property-change next nil end)))
          (push (elate--prop-run-entry pos next sig) runs)
          (setq n (1+ n)
                pos next))))
    (list :runs (vconcat (nreverse runs))
          :truncated (elate--jbool truncated))))

(defun elate--overlay-entry (ov)
  "Plist describing overlay OV: bounds plus the UI-relevant properties."
  (let ((face (overlay-get ov 'face))
        (invisible (overlay-get ov 'invisible))
        (display (overlay-get ov 'display))
        (before (overlay-get ov 'before-string))
        (after (overlay-get ov 'after-string))
        (priority (overlay-get ov 'priority)))
    (append
     (list :start (overlay-start ov) :end (overlay-end ov))
     (and face (list :face (elate--face-list face)))
     (and invisible (list :invisible (elate--clip-print invisible)))
     (and display (list :display (elate--clip-print display)))
     (and before (list :before-string
                       (elate--clip-string
                        (substring-no-properties (format "%s" before)) 500)))
     (and after (list :after-string
                      (elate--clip-string
                       (substring-no-properties (format "%s" after)) 500)))
     (and priority (list :priority (elate--clip-print priority))))))

(defun elate--overlay-dump (beg end)
  "Overlays touching BEG..END as {:overlays VECTOR :overlays-truncated BOOL}."
  (let* ((ovs (sort (overlays-in beg end)
                    (lambda (a b) (< (overlay-start a) (overlay-start b)))))
         (truncated (> (length ovs) elate--max-overlays))
         (ovs (seq-take ovs elate--max-overlays)))
    (list :overlays (vconcat (mapcar #'elate--overlay-entry ovs))
          :overlays-truncated (elate--jbool truncated))))

(defun elate--rpc-faces-at (line col &optional name)
  "Faces, text properties, and overlays at LINE:COL in buffer NAME.
LINE is 1-based, COL 0-based (clamped to the line).  :face is the
text-property face; :char-face additionally resolves overlays (what
the user actually sees); :properties lists every text property name
present at the position."
  (let ((buf (if (and name (stringp name))
                 (or (get-buffer name)
                     (error "elate: no buffer named %S" name))
               (elate--current-buffer))))
    (with-current-buffer buf
      (save-excursion
        (save-restriction
          (widen)
          (goto-char (point-min))
          (forward-line (1- (max 1 line)))
          (move-to-column (max 0 col))
          (let ((pos (point)))
            (when (and font-lock-mode (fboundp 'font-lock-ensure))
              (ignore-errors
                (font-lock-ensure (line-beginning-position)
                                  (line-end-position))))
            (pcase-let ((`(,face ,display ,invisible ,button ,field ,keymap)
                         (elate--props-signature pos)))
              (list :buffer (buffer-name)
                    :pos pos
                    :line (line-number-at-pos pos t)
                    :column (current-column)
                    :char (if (eobp) :null (char-to-string (char-after pos)))
                    :face (elate--jnull (elate--face-list face))
                    :char-face (elate--jnull
                                (elate--face-list
                                 (get-char-property pos 'face)))
                    :display (if display (elate--clip-print display) :null)
                    :invisible (if invisible
                                   (elate--clip-print invisible)
                                 :null)
                    :button (elate--jbool button)
                    :field (if field (elate--clip-print field) :null)
                    :keymap (elate--jbool keymap)
                    :properties (vconcat
                                 (let ((plist (text-properties-at pos))
                                       (names nil))
                                   (while plist
                                     (push (symbol-name (car plist)) names)
                                     (setq plist (cddr plist)))
                                   (nreverse names)))
                    :overlays (vconcat
                               (mapcar #'elate--overlay-entry
                                       (overlays-at pos)))))))))))

;;;; Popup capture

(defconst elate--max-popup-text 4096
  "Cap on the captured text of a single popup.")

(defun elate--popup-window-text (win)
  "Full buffer text shown by popup window WIN, clipped."
  (with-current-buffer (window-buffer win)
    (elate--clip-string
     (buffer-substring-no-properties (point-min) (point-max))
     elate--max-popup-text)))

(defun elate--popups (with-text)
  "Currently visible popups as a list of {:kind :buffer :text} plists.
Detects which-key (Emacs 30+), transient (Emacs 31), hydra's lv
window, corfu's child frame, company's pseudo tooltip,
completion-preview's overlay, and any other visible child frame
\(posframe & friends).  Mechanisms that are not installed simply do
not match; every probe is error-shielded.  Text capture is skipped
when WITH-TEXT is nil (the cheap \"are there popups?\" probe used by
`state')."
  (let ((found nil))
    (with-current-buffer (elate--current-buffer)
      (let ((add (lambda (kind buffer text)
                   (push (append (list :kind kind)
                                 (and buffer (list :buffer buffer))
                                 (and with-text (list :text (or text ""))))
                         found))))
        ;; which-key (ships with Emacs 30+)
        (ignore-errors
          (let* ((buf (bound-and-true-p which-key--buffer))
                 (win (and buf (buffer-live-p buf)
                           (get-buffer-window buf t))))
            (when win
              (funcall add "which-key" (buffer-name buf)
                       (and with-text (elate--popup-window-text win))))))
        ;; transient (ships with Emacs 31)
        (ignore-errors
          (let ((win (bound-and-true-p transient--window)))
            (when (and win (window-live-p win))
              (funcall add "transient" (buffer-name (window-buffer win))
                       (and with-text (elate--popup-window-text win))))))
        ;; hydra hint (lv.el)
        (ignore-errors
          (let ((win (bound-and-true-p lv-wnd)))
            (when (and win (window-live-p win))
              (funcall add "hydra-lv" (buffer-name (window-buffer win))
                       (and with-text (elate--popup-window-text win))))))
        ;; corfu's child frame
        (ignore-errors
          (let ((frame (bound-and-true-p corfu--frame)))
            (when (and frame (frame-live-p frame) (frame-visible-p frame))
              (funcall add "corfu" nil
                       (and with-text
                            (elate--popup-window-text
                             (frame-root-window frame)))))))
        ;; company's pseudo tooltip (an overlay in the current buffer)
        (ignore-errors
          (let ((ov (bound-and-true-p company-pseudo-tooltip-overlay)))
            (when (overlayp ov)
              (funcall add "company" nil
                       (and with-text
                            (elate--clip-string
                             (substring-no-properties
                              (format "%s"
                                      (or (overlay-get ov 'company-display)
                                          (overlay-get ov 'after-string)
                                          "")))
                             elate--max-popup-text))))))
        ;; completion-preview (Emacs 30+) -- buffer-local overlay
        (ignore-errors
          (let ((ov (bound-and-true-p completion-preview--overlay)))
            (when (overlayp ov)
              (funcall add "completion-preview" nil
                       (and with-text
                            (substring-no-properties
                             (format "%s"
                                     (or (overlay-get ov 'after-string)
                                         ""))))))))
        ;; any other visible child frame (posframe etc.)
        (ignore-errors
          (dolist (frame (frame-list))
            (when (and (frame-live-p frame)
                       (frame-parameter frame 'parent-frame)
                       (frame-visible-p frame)
                       (not (eq frame (bound-and-true-p corfu--frame))))
              (let ((win (frame-root-window frame)))
                (funcall add "childframe"
                         (buffer-name (window-buffer win))
                         (and with-text
                              (elate--popup-window-text win)))))))))
    (nreverse found)))

(defun elate--rpc-popups ()
  "Capture currently visible popups, with their text."
  (list :popups (vconcat (elate--popups t))))

(provide 'elate-agent)
;;; elate-agent.el ends here
