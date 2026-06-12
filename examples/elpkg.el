;;; elpkg.el --- elate clean-install example package  -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.3
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:

;; A tiny valid package used by examples/clean-install.json: one
;; autoloaded command, so the scenario can verify that the install
;; generated working autoloads.

;;; Code:

;;;###autoload
(defun elpkg-greet ()
  "Insert a greeting at point."
  (interactive)
  (insert "elpkg says hi"))

(provide 'elpkg)
;;; elpkg.el ends here
