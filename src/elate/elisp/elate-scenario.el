;;; elate-scenario.el --- read s-expression scenarios as JSON  -*- lexical-binding: t; -*-

;; Run by the elate controller in a batch Emacs:
;;
;;   emacs -Q --batch -l elate-scenario.el -f elate-scenario-to-json IN OUT
;;     reads the .eld scenario IN and writes it to OUT as JSON;
;;   emacs -Q --batch -l elate-scenario.el -f elate-scenario-check-forms IN OUT
;;     reads a JSON array of strings from IN and writes to OUT, for each,
;;     whether it can be written into an .eld file as a bare form.
;;
;; An error is written to OUT as {"error": MESSAGE, "line": N} (the
;; line when known), with exit status 1.
;;
;; Mapping (see SCRIPTING.md):
;;   (:key VALUE ...)         a list starting with a keyword -> object
;;                            (key names verbatim, without the colon)
;;   (SYMBOL ...), 'x, #'f    a Lisp form -> its source text, a string
;;   (A B ...), [A B ...]     otherwise -> array; () and nil -> null
;;   "s" -> string, 1 / 1.5 -> number, t -> true, :false -> false,
;;   :null -> null, other symbols -> their name, #s(hash-table) -> {}

;;; Code:

(define-error 'elate-scenario-error "Scenario error")

(defun elate-scenario--fail (pos format-string &rest args)
  "Signal an error at buffer position POS: FORMAT-STRING applied to ARGS."
  (signal 'elate-scenario-error
          (list (apply #'format format-string args)
                (line-number-at-pos pos))))

(defun elate-scenario--skip ()
  "Move past whitespace and comments."
  (forward-comment (buffer-size)))

(defun elate-scenario--read (start)
  "Read one datum at point, reporting reader errors at START."
  (condition-case err
      (read (current-buffer))
    (end-of-file
     (elate-scenario--fail start "unterminated expression"))
    (invalid-read-syntax
     (elate-scenario--fail (point) "invalid syntax: %s" (cadr err)))))

(defun elate-scenario--form-head-p (head)
  "Non-nil when a list starting with HEAD is a Lisp form."
  (and (symbolp head) (not (keywordp head)) (not (memq head '(nil t)))))

(defun elate-scenario--atom (value start)
  "VALUE, read at START, as the Lisp structure `json-serialize' takes."
  (cond
   ((null value) :null)
   ((memq value '(t :false :null)) value)
   ((or (stringp value) (numberp value))
    (condition-case nil
        (and (json-serialize (vector value)) value)
      (error (elate-scenario--fail
              start "%S has no JSON equivalent%s" value
              (if (stringp value) " (it holds raw bytes, not characters)"
                "")))))
   ((keywordp value) (substring (symbol-name value) 1))
   ((symbolp value) (symbol-name value))
   ((and (hash-table-p value) (zerop (hash-table-count value)))
    (make-hash-table :test #'equal))
   ((hash-table-p value)
    (elate-scenario--fail start "write an object as a plist (:key VALUE ...); \
only an empty #s(hash-table) is accepted"))
   (t (elate-scenario--fail start "cannot express %S in a scenario" value))))

(defun elate-scenario--items (close start)
  "Parse array elements up to the CLOSE character of the list at START."
  (let (items)
    (catch 'done
      (while t
        (elate-scenario--skip)
        (let ((c (char-after)))
          (cond
           ((null c) (elate-scenario--fail start "unterminated list"))
           ((eq c close) (forward-char) (throw 'done nil))
           ((memq c '(?\) ?\]))
            (elate-scenario--fail (point) "mismatched `%c'" c))
           ((elate-scenario--dot-p) (elate-scenario--dotted))
           (t (push (elate-scenario--value) items))))))
    (vconcat (nreverse items))))

(defun elate-scenario--dot-p ()
  "Non-nil when point is at the dot of a dotted pair, as the reader sees it."
  (and (eq (char-after) ?.)
       (let ((next (char-after (1+ (point)))))
         (or (null next) (<= next ?\s) (memq next '(?\" ?' ?\; ?\( ?\[ ?# ?? ?` ?,))))))

(defun elate-scenario--dotted ()
  "Signal the error for a dotted pair at point."
  (elate-scenario--fail (point) "a dotted pair has no JSON meaning (use a \
plist or a list)"))

(defun elate-scenario--plist (start)
  "Parse the keyword/value pairs of the list at START as an object."
  (let ((table (make-hash-table :test #'equal)))
    (catch 'done
      (while t
        (elate-scenario--skip)
        (let ((pos (point)) (c (char-after)))
          (cond
           ((null c) (elate-scenario--fail start "unterminated list"))
           ((eq c ?\)) (forward-char) (throw 'done nil))
           ((elate-scenario--dot-p) (elate-scenario--dotted))
           (t
            (let ((key (elate-scenario--read pos)))
              (unless (keywordp key)
                (elate-scenario--fail
                 pos "expected a keyword key, got %S (a list that starts \
with a keyword is an object: (:key VALUE ...))" key))
              (setq key (substring (symbol-name key) 1))
              (when (gethash key table)
                (elate-scenario--fail pos "duplicate key :%s" key))
              (elate-scenario--skip)
              (when (memq (char-after) '(nil ?\)))
                (elate-scenario--fail pos "key :%s has no value" key))
              (when (elate-scenario--dot-p) (elate-scenario--dotted))
              (puthash key (elate-scenario--value) table)))))))
    table))

(defun elate-scenario--value ()
  "Parse the value at point; leave point after it."
  (elate-scenario--skip)
  (let ((start (point)) (c (char-after)))
    (cond
     ((null c) (elate-scenario--fail start "unexpected end of file"))
     ((memq c '(?\) ?\])) (elate-scenario--fail start "unbalanced `%c'" c))
     ((eq c ?\[) (forward-char) (elate-scenario--items ?\] start))
     ((eq c ?\()
      (let ((head (save-excursion
                    (forward-char)
                    (elate-scenario--skip)
                    (if (eq (char-after) ?\))
                        'elate-scenario--empty
                      (elate-scenario--read (point))))))
        (cond
         ((eq head 'elate-scenario--empty)
          (elate-scenario--read start)
          :null)
         ((keywordp head) (forward-char) (elate-scenario--plist start))
         ((elate-scenario--form-head-p head)
          (elate-scenario--read start)
          (buffer-substring-no-properties start (point)))
         (t (forward-char) (elate-scenario--items ?\) start)))))
     (t
      (let ((value (elate-scenario--read start)))
        (if (consp value)               ; 'x, #'f, `(...)
            (buffer-substring-no-properties start (point))
          (elate-scenario--atom value start)))))))

(defun elate-scenario-parse ()
  "Parse the scenario in the current buffer into `json-serialize' input."
  (with-syntax-table emacs-lisp-mode-syntax-table
    (goto-char (point-min))
    (elate-scenario--skip)
    (when (eobp)
      (elate-scenario--fail (point-min) "no scenario form (empty file?)"))
    (let ((start (point)))
      (unless (and (eq (char-after) ?\()
                   (save-excursion
                     (forward-char)
                     (elate-scenario--skip)
                     (eq (char-after) ?:)))
        (elate-scenario--fail start "a scenario is one plist: \
(:steps (...) ...)"))
      (prog1 (elate-scenario--value)
        (elate-scenario--skip)
        (unless (eobp)
          (elate-scenario--fail (point) "text after the scenario form"))))))

(defun elate-scenario--insert-file (file)
  "Insert FILE, decoded as UTF-8."
  (let ((coding-system-for-read 'utf-8))
    (insert-file-contents file)))

(defun elate-scenario--write (string file)
  "Write STRING to FILE as UTF-8."
  (let ((coding-system-for-write 'utf-8-unix))
    (write-region string nil file nil 'silent)))

(defun elate-scenario--batch (fn)
  "Call FN with the command-line arguments IN and OUT, then exit.
FN returns the value to write to OUT as JSON; an error is written there
as {\"error\": MESSAGE, \"line\": N} with exit status 1."
  (let ((in (pop command-line-args-left))
        (out (pop command-line-args-left)))
    (setq command-line-args-left nil)
    (condition-case err
        (elate-scenario--write (json-serialize (funcall fn in)) out)
      (error
       (let ((table (make-hash-table :test #'equal)))
         (puthash "error" (if (eq (car err) 'elate-scenario-error)
                              (cadr err)
                            (error-message-string err))
                  table)
         (when (eq (car err) 'elate-scenario-error)
           (puthash "line" (nth 2 err) table))
         (elate-scenario--write (json-serialize table) out))
       (kill-emacs 1)))
    (kill-emacs 0)))

(defun elate-scenario-to-json ()
  "Batch entry point: convert the .eld scenario IN to JSON in OUT."
  (elate-scenario--batch
   (lambda (in)
     (with-temp-buffer
       (elate-scenario--insert-file in)
       (elate-scenario-parse)))))

(defun elate-scenario--bare-form-p (string)
  "Non-nil when STRING, written bare into a scenario, reads back as itself."
  (and (> (length string) 0)
       (not (memq (aref string 0) '(?\s ?\t ?\n ?\r ?\; ?\f)))
       (condition-case nil
           (let ((read (read-from-string string)))
             (and (= (cdr read) (length string))
                  (consp (car read))
                  (or (not (eq (aref string 0) ?\())
                      (elate-scenario--form-head-p (caar read)))))
         (error nil))))

(defun elate-scenario-check-forms ()
  "Batch entry point: which strings in the JSON array IN are bare forms."
  (elate-scenario--batch
   (lambda (in)
     (let ((strings (with-temp-buffer
                      (elate-scenario--insert-file in)
                      (json-parse-buffer :array-type 'list))))
       (vconcat (mapcar (lambda (s)
                          (if (elate-scenario--bare-form-p s) t :false))
                        strings))))))

(provide 'elate-scenario)
;;; elate-scenario.el ends here
