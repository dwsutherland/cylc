#!/usr/bin/env bash
# THIS FILE IS PART OF THE CYLC WORKFLOW ENGINE.
# Copyright (C) NIWA & British Crown (Met Office) & Contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#-------------------------------------------------------------------------------
# Test intercycle dependencies.
. "$(dirname "$0")/test_header"
#-------------------------------------------------------------------------------
if [[ -f "$TEST_SOURCE_DIR/${TEST_NAME_BASE}-find.out" ]]; then
    set_test_number 4
else
    set_test_number 3
fi
#-------------------------------------------------------------------------------
CHOSEN_WORKFLOW="$(basename "$0" | sed "s/^.*-\(.*\)\.t/\1/g")"
install_workflow "${TEST_NAME_BASE}" "${CHOSEN_WORKFLOW}"
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-validate"
run_ok "${TEST_NAME}" cylc validate "${WORKFLOW_NAME}"
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-graph"
graph_workflow "${WORKFLOW_NAME}" "${WORKFLOW_NAME}.graph.plain" \
    "20000101T00Z" "20000102T12Z"
cmp_ok "${WORKFLOW_NAME}.graph.plain" "$TEST_SOURCE_DIR/$CHOSEN_WORKFLOW/graph.plain.ref"
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-run"
workflow_run_ok "${TEST_NAME}" cylc play --reference-test --debug --no-detach "${WORKFLOW_NAME}"
#-------------------------------------------------------------------------------
if [[ -f "$TEST_SOURCE_DIR/${TEST_NAME_BASE}-find.out" ]]; then
    TEST_NAME="${TEST_NAME_BASE}-find"
    WORKFLOW_DIR="$RUN_DIR/${WORKFLOW_NAME}"
    (cd "$WORKFLOW_DIR" && find 'log/job' 'work' -type f) | sort -V >"${TEST_NAME}"
    cmp_ok "${TEST_NAME}" "$TEST_SOURCE_DIR/${TEST_NAME_BASE}-find.out"
fi
#-------------------------------------------------------------------------------
purge
