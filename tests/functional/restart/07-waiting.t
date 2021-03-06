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
# Test restarting a simple workflow with a waiting task

# TODO: this test is not very meaningful in SoD

if [[ -z ${TEST_DIR:-} ]]; then
    . "$(dirname "$0")/test_header"
fi
#-------------------------------------------------------------------------------
set_test_number 6
#-------------------------------------------------------------------------------
install_workflow "${TEST_NAME_BASE}" 'waiting'
cp "$TEST_SOURCE_DIR/lib/flow-runtime-restart.cylc" "$RUN_DIR/${WORKFLOW_NAME}/"
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-validate"
run_ok "${TEST_NAME}" cylc validate "${WORKFLOW_NAME}"
cmp_ok "${TEST_NAME}.stderr" <'/dev/null'
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-run"
workflow_run_ok "${TEST_NAME}" cylc play --debug --no-detach "${WORKFLOW_NAME}"
#-------------------------------------------------------------------------------
TEST_NAME="${TEST_NAME_BASE}-restart-run"
workflow_run_ok "${TEST_NAME}" cylc play --debug --no-detach "${WORKFLOW_NAME}"
#-------------------------------------------------------------------------------
contains_ok "$WORKFLOW_RUN_DIR/post-restart-db" <<'__DB_DUMP__'
shutdown|20130923T0000Z|1|1|succeeded
__DB_DUMP__
"${WORKFLOW_RUN_DIR}/bin/ctb-select-task-states" "${WORKFLOW_RUN_DIR}" \
    > "${TEST_DIR}/db"
contains_ok "${TEST_DIR}/db" <<'__DB_DUMP__'
finish|20130923T0000Z|1|1|succeeded
output_states|20130923T0000Z|1|1|succeeded
shutdown|20130923T0000Z|1|1|succeeded
waiting_task|20130923T0000Z|1|1|succeeded
__DB_DUMP__
#-------------------------------------------------------------------------------
purge
