@echo off
rem hs-secrets — user-level secret store — values in the OS keychain or an encrypted file, append-only ledgers (Windows launcher).
rem Generated from harness\data\launcher-templates\ by harness\scripts\launchers.py;
rem edit the template, then `python3 harness/scripts/launchers.py --write`.
rem Resolves the harness root from this script's own directory and delegates every
rem argument to harness\scripts\hs_secrets.py. POSIX twin: hs-secrets (sh).
rem enabledelayedexpansion so !errorlevel! is read AFTER python runs, not at
rem parse-time; otherwise every call would exit 0 whatever the child returned.
setlocal enabledelayedexpansion
set "bin_dir=%~dp0"
set "script=%bin_dir%..\scripts\hs_secrets.py"
if not exist "%script%" (
    echo hs-secrets: cannot find "%script%" — is the harness installed?>&2
    exit /b 2
)
rem HARNESS_PYTHON names the interpreter: a path, or a command prefix.
rem A `uv run` prefix asks the cache first; a miss runs online (see the sh twin).
if defined HARNESS_PYTHON (
    set "py=!HARNESS_PYTHON!"
    if /i "!py:~0,7!"=="uv run " (
        set "cached=uv run --offline !py:~7!"
        !cached! -c "" >nul 2>nul && set "py=!cached!"
    )
    !py! "%script%" %*
    exit /b !errorlevel!
)
where python >nul 2>nul && (
    python "%script%" %*
    exit /b !errorlevel!
)
where py >nul 2>nul && (
    py "%script%" %*
    exit /b !errorlevel!
)
echo hs-secrets: python not found — install Python 3 to run the harness CLI.>&2
exit /b 2
