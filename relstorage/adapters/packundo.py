##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""Pack/Undo implementations.
"""

import BTrees
from relstorage.adapters.interfaces import IPackUndo
from ZODB.POSException import UndoError
from zope.interface import implements
import logging
import time

try:
    IISet = BTrees.family64.II.Set
    difference = BTrees.family64.II.difference
except AttributeError:
    # Fall back to old BTrees with no special support for 64 bit integers
    from BTrees.OOBTree import OOSet as IISet
    from BTrees.OOBTree import difference

log = logging.getLogger(__name__)


class PackUndo(object):
    """Abstract base class for pack/undo"""

    verify_sane_database = False

    def __init__(self, connmanager, runner, locker, options):
        self.connmanager = connmanager
        self.runner = runner
        self.locker = locker
        self.options = options

    def choose_pack_transaction(self, pack_point):
        """Return the transaction before or at the specified pack time.

        Returns None if there is nothing to pack.
        """
        conn, cursor = self.connmanager.open()
        try:
            stmt = self._script_choose_pack_transaction
            self.runner.run_script(cursor, stmt, {'tid': pack_point})
            rows = cursor.fetchall()
            if not rows:
                # Nothing needs to be packed.
                return None
            return rows[0][0]
        finally:
            self.connmanager.close(conn, cursor)

    def _traverse_graph(self, cursor):
        """Visit the entire object graph and set the pack_object.keep flags.

        Dispatches to the implementation specified in
        self.options.pack_gc_traversal.
        """
        m = getattr(self,
            '_traverse_graph_%s' % self.options.pack_gc_traversal)
        m(cursor)

    def _traverse_graph_python(self, cursor):
        """Visit the entire object graph and set the pack_object.keep flags.

        Python implementation.
        """
        log.info("pre_pack: downloading pack_object and object_ref.")

        # Download the list of root objects to keep from pack_object.
        keep_set = IISet([0])  # set([oid])
        stmt = """
        SELECT zoid
        FROM pack_object
        WHERE keep = %(TRUE)s
        """
        self.runner.run_script_stmt(cursor, stmt)
        for from_oid, in cursor:
            keep_set.insert(from_oid)

        # Download the list of object references into all_refs.
        # Use IISets to minimize RAM consumption.
        all_refs = {}  # {from_oid: set([to_oid])}
        stmt = """
        SELECT object_ref.zoid, object_ref.to_zoid
        FROM object_ref
            JOIN pack_object ON (object_ref.zoid = pack_object.zoid)
        WHERE object_ref.tid >= pack_object.keep_tid
        ORDER BY object_ref.zoid
        """
        self.runner.run_script_stmt(cursor, stmt)
        current_oid = None
        current_refs = IISet()  # set([to_oid])
        for from_oid, to_oid in cursor:
            if current_oid is None:
                current_oid = from_oid
            elif current_oid != from_oid:
                all_refs[current_oid] = current_refs
                current_refs = IISet()
            current_refs.insert(to_oid)
        if current_oid is not None:
            all_refs[current_oid] = current_refs

        # Traverse the object graph.  Add to keep_set all of the
        # reachable OIDs.
        added_oids = IISet()
        added_oids.update(keep_set)
        pass_num = 0
        while added_oids:
            pass_num += 1
            to_visit = added_oids
            added_oids = IISet()
            for from_oid in to_visit:
                to_oids = all_refs.get(from_oid)
                if to_oids:
                    to_add = difference(to_oids, keep_set)
                    if to_add:
                        keep_set.update(to_add)
                        added_oids.update(to_add)
            log.info("pre_pack: found %d more referenced object(s) in "
                "pass %d", len(added_oids), pass_num)

        # Set pack_object.keep for all OIDs in keep_set.
        del all_refs  # Free some RAM
        log.info("pre_pack: uploading the list of reachable objects.")
        keep_list = list(keep_set)
        while keep_list:
            batch = keep_list[:100]
            keep_list = keep_list[100:]
            oids_str = ','.join(str(oid) for oid in batch)
            stmt = """
            UPDATE pack_object SET keep = %%(TRUE)s, visited = %%(TRUE)s
            WHERE zoid IN (%s)
            """ % oids_str
            self.runner.run_script_stmt(cursor, stmt)

    def _traverse_graph_sql(self, cursor):
        """Visit the entire object graph and set the pack_object.keep flags.

        SQL implementation.
        """
        # Each of the objects to be kept might refer to other objects.
        # Mark the referenced objects to be kept as well. Do this
        # repeatedly until all references have been satisfied.
        pass_num = 1
        while True:
            log.info("pre_pack: following references, pass %d", pass_num)

            # Make a list of all parent objects that still need to be
            # visited. Then set pack_object.visited for all pack_object
            # rows with keep = true.
            stmt = """
            %(TRUNCATE)s temp_pack_visit;

            INSERT INTO temp_pack_visit (zoid, keep_tid)
            SELECT zoid, keep_tid
            FROM pack_object
            WHERE keep = %(TRUE)s
                AND visited = %(FALSE)s;

            UPDATE pack_object SET visited = %(TRUE)s
            WHERE keep = %(TRUE)s
                AND visited = %(FALSE)s
            """
            self.runner.run_script(cursor, stmt)
            visit_count = cursor.rowcount

            if self.verify_sane_database:
                # Verify the update actually worked.
                # MySQL 5.1.23 fails this test; 5.1.24 passes.
                stmt = """
                SELECT 1
                FROM pack_object
                WHERE keep = %(TRUE)s AND visited = %(FALSE)s
                """
                self.runner.run_script_stmt(cursor, stmt)
                if list(cursor):
                    raise AssertionError(
                        "database failed to update pack_object")

            log.debug("pre_pack: checking references from %d object(s)",
                visit_count)

            # Visit the children of all parent objects that were
            # just visited.
            stmt = self._script_pre_pack_follow_child_refs
            self.runner.run_script(cursor, stmt)
            found_count = cursor.rowcount

            log.debug("pre_pack: found %d more referenced object(s) in "
                "pass %d", found_count, pass_num)
            if not found_count:
                # No new references detected.
                break
            else:
                pass_num += 1

    def _pause_pack(self, sleep, start):
        """Pause packing to allow concurrent commits."""
        if sleep is None:
            sleep = time.sleep
        elapsed = time.time() - start
        if elapsed == 0.0:
            # Compensate for low timer resolution by
            # assuming that at least 10 ms elapsed.
            elapsed = 0.01
        duty_cycle = self.options.pack_duty_cycle
        if duty_cycle > 0.0 and duty_cycle < 1.0:
            delay = min(self.options.pack_max_delay,
                elapsed * (1.0 / duty_cycle - 1.0))
            if delay > 0:
                log.debug('pack: sleeping %.4g second(s)', delay)
                sleep(delay)


class HistoryPreservingPackUndo(PackUndo):
    implements(IPackUndo)

    keep_history = True

    _script_choose_pack_transaction = """
        SELECT tid
        FROM transaction
        WHERE tid > 0
            AND tid <= %(tid)s
            AND packed = FALSE
        ORDER BY tid DESC
        LIMIT 1
        """

    _script_create_temp_pack_visit = """
        CREATE TEMPORARY TABLE temp_pack_visit (
            zoid BIGINT NOT NULL,
            keep_tid BIGINT NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_visit_zoid ON temp_pack_visit (zoid);
        CREATE INDEX temp_pack_keep_tid ON temp_pack_visit (keep_tid)
        """

    _script_pre_pack_follow_child_refs = """
        UPDATE pack_object SET keep = %(TRUE)s
        WHERE keep = %(FALSE)s
            AND zoid IN (
                SELECT DISTINCT to_zoid
                FROM object_ref
                    JOIN temp_pack_visit USING (zoid)
                WHERE object_ref.tid >= temp_pack_visit.keep_tid
            )
        """

    _script_create_temp_undo = """
        CREATE TEMPORARY TABLE temp_undo (
            zoid BIGINT NOT NULL,
            prev_tid BIGINT NOT NULL
        );
        CREATE UNIQUE INDEX temp_undo_zoid ON temp_undo (zoid)
        """

    _script_reset_temp_undo = "DROP TABLE temp_undo"

    _script_transaction_has_data = """
        SELECT tid
        FROM object_state
        WHERE tid = %(tid)s
        LIMIT 1
        """

    _script_pack_current_object = """
        DELETE FROM current_object
        WHERE tid = %(tid)s
            AND zoid in (
                SELECT pack_state.zoid
                FROM pack_state
                WHERE pack_state.tid = %(tid)s
            )
        """

    _script_pack_object_state = """
        DELETE FROM object_state
        WHERE tid = %(tid)s
            AND zoid in (
                SELECT pack_state.zoid
                FROM pack_state
                WHERE pack_state.tid = %(tid)s
            )
        """

    _script_pack_object_ref = """
        DELETE FROM object_refs_added
        WHERE tid IN (
            SELECT tid
            FROM transaction
            WHERE empty = %(TRUE)s
            );
        DELETE FROM object_ref
        WHERE tid IN (
            SELECT tid
            FROM transaction
            WHERE empty = %(TRUE)s
            )
        """

    def verify_undoable(self, cursor, undo_tid):
        """Raise UndoError if it is not safe to undo the specified txn."""
        stmt = """
        SELECT 1 FROM transaction
        WHERE tid = %(undo_tid)s
            AND packed = %(FALSE)s
        """
        self.runner.run_script_stmt(cursor, stmt, {'undo_tid': undo_tid})
        if not cursor.fetchall():
            raise UndoError("Transaction not found or packed")

        # Rule: we can undo an object if the object's state in the
        # transaction to undo matches the object's current state.
        # If any object in the transaction does not fit that rule,
        # refuse to undo.
        # (Note that this prevents conflict-resolving undo as described
        # by ZODB.tests.ConflictResolution.ConflictResolvingTransUndoStorage.
        # Do people need that? If so, we can probably support it, but it
        # will require additional code.)
        stmt = """
        SELECT prev_os.zoid, current_object.tid
        FROM object_state prev_os
            JOIN object_state cur_os ON (prev_os.zoid = cur_os.zoid)
            JOIN current_object ON (cur_os.zoid = current_object.zoid
                AND cur_os.tid = current_object.tid)
        WHERE prev_os.tid = %(undo_tid)s
            AND cur_os.md5 != prev_os.md5
        """
        self.runner.run_script_stmt(cursor, stmt, {'undo_tid': undo_tid})
        if cursor.fetchmany():
            raise UndoError(
                "Some data were modified by a later transaction")

        # Rule: don't allow the creation of the root object to
        # be undone.  It's hard to get it back.
        stmt = """
        SELECT 1
        FROM object_state
        WHERE tid = %(undo_tid)s
            AND zoid = 0
            AND prev_tid = 0
        """
        self.runner.run_script_stmt(cursor, stmt, {'undo_tid': undo_tid})
        if cursor.fetchall():
            raise UndoError("Can't undo the creation of the root object")


    def undo(self, cursor, undo_tid, self_tid):
        """Undo a transaction.

        Parameters: "undo_tid", the integer tid of the transaction to undo,
        and "self_tid", the integer tid of the current transaction.

        Returns the states copied forward by the undo operation as a
        list of (oid, old_tid).
        """
        stmt = self._script_create_temp_undo
        if stmt:
            self.runner.run_script(cursor, stmt)

        stmt = """
        DELETE FROM temp_undo;

        -- Put into temp_undo the list of objects to be undone and
        -- the tid of the transaction that has the undone state.
        INSERT INTO temp_undo (zoid, prev_tid)
        SELECT zoid, prev_tid
        FROM object_state
        WHERE tid = %(undo_tid)s;

        -- Override previous undo operations within this transaction
        -- by resetting the current_object pointer and deleting
        -- copied states from object_state.
        UPDATE current_object
        SET tid = (
                SELECT prev_tid
                FROM object_state
                WHERE zoid = current_object.zoid
                    AND tid = %(self_tid)s
            )
        WHERE zoid IN (SELECT zoid FROM temp_undo)
            AND tid = %(self_tid)s;

        DELETE FROM object_state
        WHERE zoid IN (SELECT zoid FROM temp_undo)
            AND tid = %(self_tid)s;

        -- Copy old states forward.
        INSERT INTO object_state (zoid, tid, prev_tid, md5, state)
        SELECT temp_undo.zoid, %(self_tid)s, current_object.tid,
            md5, state
        FROM temp_undo
            JOIN current_object ON (temp_undo.zoid = current_object.zoid)
            LEFT JOIN object_state
                ON (object_state.zoid = temp_undo.zoid
                    AND object_state.tid = temp_undo.prev_tid);

        -- Copy old blob chunks forward.
        INSERT INTO blob_chunk (zoid, tid, chunk_num, chunk)
        SELECT temp_undo.zoid, %(self_tid)s, chunk_num, chunk
        FROM temp_undo
            JOIN blob_chunk
                ON (blob_chunk.zoid = temp_undo.zoid
                    AND blob_chunk.tid = temp_undo.prev_tid);

        -- List the copied states.
        SELECT zoid, prev_tid FROM temp_undo
        """
        self.runner.run_script(cursor, stmt,
            {'undo_tid': undo_tid, 'self_tid': self_tid})
        res = list(cursor)

        stmt = self._script_reset_temp_undo
        if stmt:
            self.runner.run_script(cursor, stmt)

        return res

    def on_filling_object_refs(self):
        """Test injection point"""

    def fill_object_refs(self, conn, cursor, get_references):
        """Update the object_refs table by analyzing new transactions."""
        stmt = """
        SELECT transaction.tid
        FROM transaction
            LEFT JOIN object_refs_added
                ON (transaction.tid = object_refs_added.tid)
        WHERE object_refs_added.tid IS NULL
        ORDER BY transaction.tid
        """
        self.runner.run_script_stmt(cursor, stmt)
        tids = [tid for (tid,) in cursor]
        log_at = time.time() + 60
        if tids:
            self.on_filling_object_refs()
            tid_count = len(tids)
            txns_done = 0
            log.info("analyzing references from objects in %d "
                "transaction(s)", tid_count)
            for tid in tids:
                self._add_refs_for_tid(cursor, tid, get_references)
                txns_done += 1
                now = time.time()
                if now >= log_at:
                    # save the work done so far
                    conn.commit()
                    log_at = now + 60
                    log.info(
                        "transactions analyzed: %d/%d", txns_done, tid_count)
            conn.commit()
            log.info("transactions analyzed: %d/%d", txns_done, tid_count)

    def _add_refs_for_tid(self, cursor, tid, get_references):
        """Fill object_refs with all states for a transaction.

        Returns the number of references added.
        """
        log.debug("pre_pack: transaction %d: computing references ", tid)
        from_count = 0

        stmt = """
        SELECT zoid, state
        FROM object_state
        WHERE tid = %(tid)s
        """
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})

        replace_rows = []
        add_rows = []  # [(from_oid, tid, to_oid)]
        for from_oid, state in cursor:
            replace_rows.append((from_oid,))
            if hasattr(state, 'read'):
                # Oracle
                state = state.read()
            if state:
                from_count += 1
                try:
                    to_oids = get_references(str(state))
                except:
                    log.error("pre_pack: can't unpickle "
                        "object %d in transaction %d; state length = %d" % (
                        from_oid, tid, len(state)))
                    raise
                for to_oid in to_oids:
                    add_rows.append((from_oid, tid, to_oid))

        # A previous pre-pack may have been interrupted.  Delete rows
        # from the interrupted attempt.
        stmt = "DELETE FROM object_ref WHERE tid = %(tid)s"
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})

        # Add the new references.
        stmt = """
        INSERT INTO object_ref (zoid, tid, to_zoid)
        VALUES (%s, %s, %s)
        """
        self.runner.run_many(cursor, stmt, add_rows)

        # The references have been computed for this transaction.
        stmt = """
        INSERT INTO object_refs_added (tid)
        VALUES (%(tid)s)
        """
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})

        to_count = len(add_rows)
        log.debug("pre_pack: transaction %d: has %d reference(s) "
            "from %d object(s)", tid, to_count, from_count)
        return to_count

    def pre_pack(self, pack_tid, get_references):
        """Decide what to pack.

        pack_tid specifies the most recent transaction to pack.

        get_references is a function that accepts a pickled state and
        returns a set of OIDs that state refers to.

        The self.options.pack_gc flag indicates whether
        to run garbage collection.
        If pack_gc is false, at least one revision of every object is kept,
        even if nothing refers to it.  Packing with pack_gc disabled can be
        much faster.
        """
        conn, cursor = self.connmanager.open_for_pre_pack()
        try:
            try:
                if self.options.pack_gc:
                    log.info("pre_pack: start with gc enabled")
                    self._pre_pack_with_gc(
                        conn, cursor, pack_tid, get_references)
                else:
                    log.info("pre_pack: start without gc")
                    self._pre_pack_without_gc(
                        conn, cursor, pack_tid)
                conn.commit()

                log.info("pre_pack: enumerating states to pack")
                stmt = "%(TRUNCATE)s pack_state"
                self.runner.run_script_stmt(cursor, stmt)
                to_remove = 0

                if self.options.pack_gc:
                    # Pack objects with the keep flag set to false.
                    stmt = """
                    INSERT INTO pack_state (tid, zoid)
                    SELECT tid, zoid
                    FROM object_state
                        JOIN pack_object USING (zoid)
                    WHERE keep = %(FALSE)s
                        AND tid > 0
                        AND tid <= %(pack_tid)s
                    """
                    self.runner.run_script_stmt(
                        cursor, stmt, {'pack_tid': pack_tid})
                    to_remove += cursor.rowcount

                # Pack object states with the keep flag set to true.
                stmt = """
                INSERT INTO pack_state (tid, zoid)
                SELECT tid, zoid
                FROM object_state
                    JOIN pack_object USING (zoid)
                WHERE keep = %(TRUE)s
                    AND tid > 0
                    AND tid != keep_tid
                    AND tid <= %(pack_tid)s
                """
                self.runner.run_script_stmt(
                    cursor, stmt, {'pack_tid':pack_tid})
                to_remove += cursor.rowcount

                log.info("pre_pack: enumerating transactions to pack")
                stmt = "%(TRUNCATE)s pack_state_tid"
                self.runner.run_script_stmt(cursor, stmt)
                stmt = """
                INSERT INTO pack_state_tid (tid)
                SELECT DISTINCT tid
                FROM pack_state
                """
                cursor.execute(stmt)

                log.info("pre_pack: will remove %d object state(s)",
                    to_remove)

            except:
                log.exception("pre_pack: failed")
                conn.rollback()
                raise
            else:
                log.info("pre_pack: finished successfully")
                conn.commit()
        finally:
            self.connmanager.close(conn, cursor)


    def _pre_pack_without_gc(self, conn, cursor, pack_tid):
        """Determine what to pack, without garbage collection.

        With garbage collection disabled, there is no need to follow
        object references.
        """
        # Fill the pack_object table with OIDs, but configure them
        # all to be kept by setting keep to true.
        log.debug("pre_pack: populating pack_object")
        stmt = """
        %(TRUNCATE)s pack_object;

        INSERT INTO pack_object (zoid, keep, keep_tid)
        SELECT zoid, %(TRUE)s, MAX(tid)
        FROM object_state
        WHERE tid > 0 AND tid <= %(pack_tid)s
        GROUP BY zoid
        """
        self.runner.run_script(cursor, stmt, {'pack_tid': pack_tid})


    def _pre_pack_with_gc(self, conn, cursor, pack_tid, get_references):
        """Determine what to pack, with garbage collection.
        """
        stmt = self._script_create_temp_pack_visit
        if stmt:
            self.runner.run_script(cursor, stmt)

        self.fill_object_refs(conn, cursor, get_references)

        log.info("pre_pack: filling the pack_object table")
        # Fill the pack_object table with OIDs that either will be
        # removed (if nothing references the OID) or whose history will
        # be cut.
        stmt = """
        %(TRUNCATE)s pack_object;

        INSERT INTO pack_object (zoid, keep, keep_tid)
        SELECT zoid, %(FALSE)s, MAX(tid)
        FROM object_state
        WHERE tid > 0 AND tid <= %(pack_tid)s
        GROUP BY zoid;

        -- Keep the root object.
        UPDATE pack_object SET keep = %(TRUE)s
        WHERE zoid = 0;

        -- Keep objects that have been revised since pack_tid.
        -- Use temp_pack_visit for temporary state; otherwise MySQL 5 chokes.
        INSERT INTO temp_pack_visit (zoid, keep_tid)
        SELECT zoid, 0
        FROM current_object
        WHERE tid > %(pack_tid)s;

        UPDATE pack_object SET keep = %(TRUE)s
        WHERE zoid IN (
            SELECT zoid
            FROM temp_pack_visit
        );

        %(TRUNCATE)s temp_pack_visit;

        -- Keep objects that are still referenced by object states in
        -- transactions that will not be packed.
        -- Use temp_pack_visit for temporary state; otherwise MySQL 5 chokes.
        INSERT INTO temp_pack_visit (zoid, keep_tid)
        SELECT DISTINCT to_zoid, 0
        FROM object_ref
        WHERE tid > %(pack_tid)s;

        UPDATE pack_object SET keep = %(TRUE)s
        WHERE zoid IN (
            SELECT zoid
            FROM temp_pack_visit
        );

        %(TRUNCATE)s temp_pack_visit;
        """
        self.runner.run_script(cursor, stmt, {'pack_tid': pack_tid})

        # Traverse the graph, setting the 'keep' flags in pack_object
        self._traverse_graph(cursor)


    def pack(self, pack_tid, sleep=None, packed_func=None):
        """Pack.  Requires the information provided by pre_pack."""

        # Read committed mode is sufficient.
        conn, cursor = self.connmanager.open()
        try:
            try:
                stmt = """
                SELECT transaction.tid,
                    CASE WHEN packed = %(TRUE)s THEN 1 ELSE 0 END,
                    CASE WHEN pack_state_tid.tid IS NOT NULL THEN 1 ELSE 0 END
                FROM transaction
                    LEFT JOIN pack_state_tid ON (
                        transaction.tid = pack_state_tid.tid)
                WHERE transaction.tid > 0
                    AND transaction.tid <= %(pack_tid)s
                    AND (packed = %(FALSE)s OR pack_state_tid.tid IS NOT NULL)
                """
                self.runner.run_script_stmt(
                    cursor, stmt, {'pack_tid': pack_tid})
                tid_rows = list(cursor)
                tid_rows.sort()  # oldest first

                log.info("pack: will pack %d transaction(s)", len(tid_rows))

                stmt = self._script_create_temp_pack_visit
                if stmt:
                    self.runner.run_script(cursor, stmt)

                # Hold the commit lock while packing to prevent deadlocks.
                # Pack in small batches of transactions in order to minimize
                # the interruption of concurrent write operations.
                start = time.time()
                packed_list = []
                self.locker.hold_commit_lock(cursor)
                for tid, packed, has_removable in tid_rows:
                    self._pack_transaction(
                        cursor, pack_tid, tid, packed, has_removable,
                        packed_list)
                    if time.time() >= start + self.options.pack_batch_timeout:
                        conn.commit()
                        if packed_func is not None:
                            for oid, tid in packed_list:
                                packed_func(oid, tid)
                        del packed_list[:]
                        self.locker.release_commit_lock(cursor)
                        self._pause_pack(sleep, start)
                        self.locker.hold_commit_lock(cursor)
                        start = time.time()
                if packed_func is not None:
                    for oid, tid in packed_list:
                        packed_func(oid, tid)
                packed_list = None

                self._pack_cleanup(conn, cursor)

            except:
                log.exception("pack: failed")
                conn.rollback()
                raise

            else:
                log.info("pack: finished successfully")
                conn.commit()

        finally:
            self.connmanager.close(conn, cursor)


    def _pack_transaction(self, cursor, pack_tid, tid, packed,
            has_removable, packed_list):
        """Pack one transaction.  Requires populated pack tables."""
        log.debug("pack: transaction %d: packing", tid)
        removed_objects = 0
        removed_states = 0

        if has_removable:
            stmt = self._script_pack_current_object
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            removed_objects = cursor.rowcount

            stmt = self._script_pack_object_state
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            removed_states = cursor.rowcount

            # Terminate prev_tid chains
            stmt = """
            UPDATE object_state SET prev_tid = 0
            WHERE prev_tid = %(tid)s
                AND tid <= %(pack_tid)s
            """
            self.runner.run_script_stmt(cursor, stmt,
                {'pack_tid': pack_tid, 'tid': tid})

            stmt = """
            SELECT pack_state.zoid
            FROM pack_state
            WHERE pack_state.tid = %(tid)s
            """
            self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
            for (oid,) in cursor:
                packed_list.append((oid, tid))

        # Find out whether the transaction is empty
        stmt = self._script_transaction_has_data
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})
        empty = not list(cursor)

        # mark the transaction packed and possibly empty
        if empty:
            clause = 'empty = %(TRUE)s'
            state = 'empty'
        else:
            clause = 'empty = %(FALSE)s'
            state = 'not empty'
        stmt = "UPDATE transaction SET packed = %(TRUE)s, " + clause
        stmt += " WHERE tid = %(tid)s"
        self.runner.run_script_stmt(cursor, stmt, {'tid': tid})

        log.debug(
            "pack: transaction %d (%s): removed %d object(s) and %d state(s)",
            tid, state, removed_objects, removed_states)


    def _pack_cleanup(self, conn, cursor):
        """Remove unneeded table rows after packing"""
        # commit the work done so far
        conn.commit()
        self.locker.release_commit_lock(cursor)
        self.locker.hold_commit_lock(cursor)
        log.info("pack: cleaning up")

        log.debug("pack: removing unused object references")
        stmt = self._script_pack_object_ref
        self.runner.run_script(cursor, stmt)

        log.debug("pack: removing empty packed transactions")
        stmt = """
        DELETE FROM transaction
        WHERE packed = %(TRUE)s
            AND empty = %(TRUE)s
        """
        self.runner.run_script_stmt(cursor, stmt)

        # perform cleanup that does not require the commit lock
        conn.commit()
        self.locker.release_commit_lock(cursor)

        log.debug("pack: clearing temporary pack state")
        for _table in ('pack_object', 'pack_state', 'pack_state_tid'):
            stmt = '%(TRUNCATE)s ' + _table
            self.runner.run_script_stmt(cursor, stmt)


class MySQLHistoryPreservingPackUndo(HistoryPreservingPackUndo):

    # Work around a MySQL performance bug by avoiding an expensive subquery.
    # See: http://mail.zope.org/pipermail/zodb-dev/2008-May/011880.html
    #      http://bugs.mysql.com/bug.php?id=28257
    _script_create_temp_pack_visit = """
        CREATE TEMPORARY TABLE temp_pack_visit (
            zoid BIGINT UNSIGNED NOT NULL,
            keep_tid BIGINT UNSIGNED NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_visit_zoid ON temp_pack_visit (zoid);
        CREATE INDEX temp_pack_keep_tid ON temp_pack_visit (keep_tid);
        CREATE TEMPORARY TABLE temp_pack_child (
            zoid BIGINT UNSIGNED NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_child_zoid ON temp_pack_child (zoid);
        """

    # Note: UPDATE must be the last statement in the script
    # because it returns a value.
    _script_pre_pack_follow_child_refs = """
        %(TRUNCATE)s temp_pack_child;

        INSERT IGNORE INTO temp_pack_child
        SELECT to_zoid
        FROM object_ref
            JOIN temp_pack_visit USING (zoid)
        WHERE object_ref.tid >= temp_pack_visit.keep_tid;

        -- MySQL-specific syntax for table join in update
        UPDATE pack_object, temp_pack_child SET keep = %(TRUE)s
        WHERE keep = %(FALSE)s
            AND pack_object.zoid = temp_pack_child.zoid;
        """

    # MySQL optimizes deletion far better when using a join syntax.
    _script_pack_current_object = """
        DELETE FROM current_object
        USING current_object
            JOIN pack_state USING (zoid, tid)
        WHERE current_object.tid = %(tid)s
        """

    _script_pack_object_state = """
        DELETE FROM object_state
        USING object_state
            JOIN pack_state USING (zoid, tid)
        WHERE object_state.tid = %(tid)s
        """

    _script_pack_object_ref = """
        DELETE FROM object_refs_added
        USING object_refs_added
            JOIN transaction USING (tid)
        WHERE transaction.empty = true;

        DELETE FROM object_ref
        USING object_ref
            JOIN transaction USING (tid)
        WHERE transaction.empty = true
        """

    _script_create_temp_undo = """
        CREATE TEMPORARY TABLE temp_undo (
            zoid BIGINT UNSIGNED NOT NULL,
            prev_tid BIGINT UNSIGNED NOT NULL
        );
        CREATE UNIQUE INDEX temp_undo_zoid ON temp_undo (zoid)
        """


class OracleHistoryPreservingPackUndo(HistoryPreservingPackUndo):

    _script_choose_pack_transaction = """
        SELECT MAX(tid)
        FROM transaction
        WHERE tid > 0
            AND tid <= %(tid)s
            AND packed = 'N'
        """

    _script_create_temp_pack_visit = None
    _script_create_temp_undo = None
    _script_reset_temp_undo = "DELETE FROM temp_undo"

    _script_transaction_has_data = """
        SELECT DISTINCT tid
        FROM object_state
        WHERE tid = %(tid)s
        """


class HistoryFreePackUndo(PackUndo):
    implements(IPackUndo)

    keep_history = False

    _script_choose_pack_transaction = """
        SELECT tid
        FROM object_state
        WHERE tid > 0
            AND tid <= %(tid)s
        ORDER BY tid DESC
        LIMIT 1
        """

    _script_create_temp_pack_visit = """
        CREATE TEMPORARY TABLE temp_pack_visit (
            zoid BIGINT NOT NULL,
            keep_tid BIGINT NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_visit_zoid ON temp_pack_visit (zoid);
        CREATE INDEX temp_pack_keep_tid ON temp_pack_visit (keep_tid)
        """

    _script_pre_pack_follow_child_refs = """
        UPDATE pack_object SET keep = %(TRUE)s
        WHERE keep = %(FALSE)s
            AND zoid IN (
                SELECT DISTINCT to_zoid
                FROM object_ref
                    JOIN temp_pack_visit USING (zoid)
            )
        """

    def verify_undoable(self, cursor, undo_tid):
        """Raise UndoError if it is not safe to undo the specified txn."""
        raise UndoError("Undo is not supported by this storage")

    def undo(self, cursor, undo_tid, self_tid):
        """Undo a transaction.

        Parameters: "undo_tid", the integer tid of the transaction to undo,
        and "self_tid", the integer tid of the current transaction.

        Returns the list of OIDs undone.
        """
        raise UndoError("Undo is not supported by this storage")

    def on_filling_object_refs(self):
        """Test injection point"""

    def fill_object_refs(self, conn, cursor, get_references):
        """Update the object_refs table by analyzing new object states.

        Note that ZODB connections can change the object states while this
        method is running, possibly obscuring object references,
        so this method runs repeatedly until it detects no changes between
        two passes.
        """
        holding_commit = False
        attempt = 0
        while True:
            attempt += 1
            if attempt >= 3 and not holding_commit:
                # Starting with the third attempt, hold the commit lock
                # to prevent changes.
                holding_commit = True
                self.locker.hold_commit_lock(cursor)

            stmt = """
            SELECT object_state.zoid FROM object_state
                LEFT JOIN object_refs_added
                    ON (object_state.zoid = object_refs_added.zoid)
            WHERE object_refs_added.tid IS NULL
                OR object_refs_added.tid != object_state.tid
            ORDER BY object_state.zoid
            """
            self.runner.run_script_stmt(cursor, stmt)
            oids = [oid for (oid,) in cursor]
            log_at = time.time() + 60
            if oids:
                if attempt == 1:
                    self.on_filling_object_refs()
                oid_count = len(oids)
                oids_done = 0
                log.info("analyzing references from %d object(s)", oid_count)
                while oids:
                    batch = oids[:100]
                    oids = oids[100:]
                    self._add_refs_for_oids(cursor, batch, get_references)
                    oids_done += len(batch)
                    now = time.time()
                    if now >= log_at:
                        # Save the work done so far.
                        conn.commit()
                        log_at = now + 60
                        log.info(
                            "objects analyzed: %d/%d", oids_done, oid_count)
                conn.commit()
                log.info(
                    "objects analyzed: %d/%d", oids_done, oid_count)
            else:
                # No changes since last pass.
                break

    def _add_refs_for_oids(self, cursor, oids, get_references):
        """Fill object_refs with the states for some objects.

        Returns the number of references added.
        """
        to_count = 0
        oid_list = ','.join(str(oid) for oid in oids)

        stmt = """
        SELECT zoid, tid, state
        FROM object_state
        WHERE zoid IN (%s)
        """ % oid_list
        self.runner.run_script_stmt(cursor, stmt)

        add_objects = []
        add_refs = []
        for from_oid, tid, state in cursor:
            if hasattr(state, 'read'):
                # Oracle
                state = state.read()
            add_objects.append((from_oid, tid))
            if state:
                try:
                    to_oids = get_references(str(state))
                except:
                    log.error("pre_pack: can't unpickle "
                        "object %d in transaction %d; state length = %d" % (
                        from_oid, tid, len(state)))
                    raise
                for to_oid in to_oids:
                    add_refs.append((from_oid, tid, to_oid))

        if not add_objects:
            return 0

        stmt = "DELETE FROM object_refs_added WHERE zoid IN (%s)" % oid_list
        self.runner.run_script_stmt(cursor, stmt)
        stmt = "DELETE FROM object_ref WHERE zoid IN (%s)" % oid_list
        self.runner.run_script_stmt(cursor, stmt)

        stmt = """
        INSERT INTO object_ref (zoid, tid, to_zoid) VALUES (%s, %s, %s)
        """
        self.runner.run_many(cursor, stmt, add_refs)

        stmt = """
        INSERT INTO object_refs_added (zoid, tid) VALUES (%s, %s)
        """
        self.runner.run_many(cursor, stmt, add_objects)

        return len(add_refs)

    def pre_pack(self, pack_tid, get_references):
        """Decide what the garbage collector should delete.

        Objects created or modified after pack_tid will not be
        garbage collected.

        get_references is a function that accepts a pickled state and
        returns a set of OIDs that state refers to.

        The self.options.pack_gc flag indicates whether to run garbage
        collection.  If pack_gc is false, this method does nothing.
        """
        if not self.options.pack_gc:
            log.warning("pre_pack: garbage collection is disabled on a "
                "history-free storage, so doing nothing")
            return

        conn, cursor = self.connmanager.open_for_pre_pack()
        try:
            try:
                self._pre_pack_main(conn, cursor, pack_tid, get_references)
            except:
                log.exception("pre_pack: failed")
                conn.rollback()
                raise
            else:
                conn.commit()
                log.info("pre_pack: finished successfully")
        finally:
            self.connmanager.close(conn, cursor)


    def _pre_pack_main(self, conn, cursor, pack_tid, get_references):
        """Determine what to garbage collect.
        """
        stmt = self._script_create_temp_pack_visit
        if stmt:
            self.runner.run_script(cursor, stmt)

        self.fill_object_refs(conn, cursor, get_references)

        log.info("pre_pack: filling the pack_object table")
        # Fill the pack_object table with all known OIDs.
        stmt = """
        %(TRUNCATE)s pack_object;

        INSERT INTO pack_object (zoid, keep, keep_tid)
        SELECT zoid, %(FALSE)s, tid
        FROM object_state;

        -- Keep the root object.
        UPDATE pack_object SET keep = %(TRUE)s
        WHERE zoid = 0;

        -- Keep objects that have been revised since pack_tid.
        UPDATE pack_object SET keep = %(TRUE)s
        WHERE keep_tid > %(pack_tid)s;
        """
        self.runner.run_script(cursor, stmt, {'pack_tid': pack_tid})

        # Traverse the graph, setting the 'keep' flags in pack_object
        self._traverse_graph(cursor)


    def pack(self, pack_tid, sleep=None, packed_func=None):
        """Run garbage collection.

        Requires the information provided by pre_pack.
        """
        # Read committed mode is sufficient.
        conn, cursor = self.connmanager.open()
        try:
            try:
                stmt = """
                SELECT zoid, keep_tid
                FROM pack_object
                WHERE keep = %(FALSE)s
                """
                self.runner.run_script_stmt(cursor, stmt)
                to_remove = list(cursor)

                log.info("pack: will remove %d object(s)", len(to_remove))

                # Hold the commit lock while packing to prevent deadlocks.
                # Pack in small batches of transactions in order to minimize
                # the interruption of concurrent write operations.
                start = time.time()
                packed_list = []
                self.locker.hold_commit_lock(cursor)

                while to_remove:
                    items = to_remove[:100]
                    del to_remove[:100]
                    stmt = """
                    DELETE FROM object_state
                    WHERE zoid = %s AND tid = %s
                    """
                    self.runner.run_many(cursor, stmt, items)
                    packed_list.extend(items)

                    if time.time() >= start + self.options.pack_batch_timeout:
                        conn.commit()
                        if packed_func is not None:
                            for oid, tid in packed_list:
                                packed_func(oid, tid)
                        del packed_list[:]
                        self.locker.release_commit_lock(cursor)
                        self._pause_pack(sleep, start)
                        self.locker.hold_commit_lock(cursor)
                        start = time.time()

                if packed_func is not None:
                    for oid, tid in packed_list:
                        packed_func(oid, tid)
                packed_list = None

                self._pack_cleanup(conn, cursor)

            except:
                log.exception("pack: failed")
                conn.rollback()
                raise

            else:
                log.info("pack: finished successfully")
                conn.commit()

        finally:
            self.connmanager.close(conn, cursor)


    def _pack_cleanup(self, conn, cursor):
        # commit the work done so far
        conn.commit()
        self.locker.release_commit_lock(cursor)
        self.locker.hold_commit_lock(cursor)
        log.info("pack: cleaning up")

        stmt = """
        DELETE FROM object_refs_added
        WHERE zoid IN (
            SELECT zoid
            FROM pack_object
            WHERE keep = %(FALSE)s
        );

        DELETE FROM object_ref
        WHERE zoid IN (
            SELECT zoid
            FROM pack_object
            WHERE keep = %(FALSE)s
        );

        %(TRUNCATE)s pack_object
        """
        self.runner.run_script(cursor, stmt)


class MySQLHistoryFreePackUndo(HistoryFreePackUndo):

    _script_create_temp_pack_visit = """
        CREATE TEMPORARY TABLE temp_pack_visit (
            zoid BIGINT UNSIGNED NOT NULL,
            keep_tid BIGINT UNSIGNED NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_visit_zoid ON temp_pack_visit (zoid);
        CREATE TEMPORARY TABLE temp_pack_child (
            zoid BIGINT UNSIGNED NOT NULL
        );
        CREATE UNIQUE INDEX temp_pack_child_zoid ON temp_pack_child (zoid);
        """

    # Note: UPDATE must be the last statement in the script
    # because it returns a value.
    _script_pre_pack_follow_child_refs = """
        %(TRUNCATE)s temp_pack_child;

        INSERT IGNORE INTO temp_pack_child
        SELECT to_zoid
        FROM object_ref
            JOIN temp_pack_visit USING (zoid);

        -- MySQL-specific syntax for table join in update
        UPDATE pack_object, temp_pack_child SET keep = %(TRUE)s
        WHERE keep = %(FALSE)s
            AND pack_object.zoid = temp_pack_child.zoid;
        """


class OracleHistoryFreePackUndo(HistoryFreePackUndo):

    _script_choose_pack_transaction = """
        SELECT MAX(tid)
        FROM object_state
        WHERE tid > 0
            AND tid <= %(tid)s
        """

    _script_create_temp_pack_visit = None
