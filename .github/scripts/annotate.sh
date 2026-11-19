#!/usr/bin/env bash
# annotate.sh TITLE FILE
# Shows the end of a text file as a notice on the workflow run, so results
# can be read from the run page (or the API) without downloading logs.
title="$1"
file="$2"
body="$(tail -c 50000 "$file" 2>/dev/null || echo "(no output file)")"
body="${body//'%'/%25}"
body="${body//$'\r'/%0D}"
body="${body//$'\n'/%0A}"
echo "::notice title=${title}::${body}"
