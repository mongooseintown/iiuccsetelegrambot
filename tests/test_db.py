"""
Automated unit tests for SQLite database operations in db.py.
"""

import os
import uuid
import unittest
import db


class TestDatabaseOperations(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.test_db = f"test_{uuid.uuid4().hex[:8]}.db"
        await db.init_db(self.test_db)

    async def asyncTearDown(self):
        for f in [self.test_db, f"{self.test_db}-wal", f"{self.test_db}-shm"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    async def test_admin_claim_and_idempotency(self):
        # Initial: no admins
        self.assertFalse(await db.has_admins(self.test_db))
        self.assertFalse(await db.is_admin(111, self.test_db))

        # First claim succeeds
        claimed = await db.claim_admin(111, "admin1", "Owner Admin", self.test_db)
        self.assertTrue(claimed)
        self.assertTrue(await db.has_admins(self.test_db))
        self.assertTrue(await db.is_admin(111, self.test_db))

        # Second claim fails (only 1st owner allowed via bootstrap code)
        second_claimed = await db.claim_admin(222, "admin2", "Second Guy", self.test_db)
        self.assertFalse(second_claimed)
        self.assertFalse(await db.is_admin(222, self.test_db))

    async def test_semester_and_course_registration(self):
        # Add semester
        await db.add_semester("S1", "Semester 1", self.test_db)
        sem = await db.get_semester_by_code("S1", self.test_db)
        self.assertIsNotNone(sem)
        self.assertEqual(sem["name"], "Semester 1")

        # Register course in target group
        course = await db.register_course(
            semester_id=sem["id"],
            code="CSE101",
            name="Data Structures",
            chat_id=-1001234567890,
            invite_link="https://t.me/+join_req_link",
            db_path=self.test_db
        )
        self.assertIsNotNone(course)
        self.assertEqual(course["code"], "CSE101")
        self.assertEqual(course["chat_id"], -1001234567890)

        # Retrieve course
        by_chat = await db.get_course_by_chat_id(-1001234567890, self.test_db)
        self.assertIsNotNone(by_chat)
        self.assertEqual(by_chat["id"], course["id"])

        courses = await db.get_courses_by_semester(sem["id"], active_only=True, db_path=self.test_db)
        self.assertEqual(len(courses), 1)

        # Update course in same group
        updated = await db.register_course(
            semester_id=sem["id"],
            code="CSE101-NEW",
            name="Advanced Data Structures",
            chat_id=-1001234567890,
            invite_link="https://t.me/+updated_link",
            db_path=self.test_db
        )
        self.assertEqual(updated["code"], "CSE101-NEW")
        self.assertEqual(updated["invite_link"], "https://t.me/+updated_link")

    async def test_student_menu_tracking(self):
        await db.add_semester("S2", "Semester 2", self.test_db)
        sem = await db.get_semester_by_code("S2", self.test_db)

        # Upsert student
        await db.upsert_student(
            telegram_id=55555,
            username="stud55",
            full_name="Student 55",
            db_path=self.test_db
        )
        student = await db.get_student(55555, self.test_db)
        self.assertIsNotNone(student)
        self.assertEqual(student["full_name"], "Student 55")
        self.assertIsNone(student["selected_semester_id"])

        # Update semester & menu message id
        await db.set_student_semester(55555, sem["id"], self.test_db)
        await db.set_student_menu_message(55555, 999123, self.test_db)

        student_updated = await db.get_student(55555, self.test_db)
        self.assertEqual(student_updated["selected_semester_id"], sem["id"])
        self.assertEqual(student_updated["last_menu_message_id"], 999123)

    async def test_join_request_lifecycle(self):
        # Setup semester & course
        await db.add_semester("S1", "Semester 1", self.test_db)
        sem = await db.get_semester_by_code("S1", self.test_db)
        course = await db.register_course(
            sem["id"], "CSE101", "Data Structures", -1001234567890, "https://t.me/+link", self.test_db
        )

        user_id = 99999
        chat_id = -1001234567890

        # Initial state: available
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "available")

        # Chat join request received -> pending
        req_id = await db.record_join_request(chat_id, user_id, course["id"], "pending", self.test_db)
        self.assertIsNotNone(req_id)
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "pending")

        pending_list = await db.get_pending_requests(self.test_db)
        self.assertEqual(len(pending_list), 1)

        # Admin approves
        await db.claim_admin(111, "admin", "Admin", self.test_db)
        updated = await db.update_join_request_status(req_id, "approved", processed_by=111, db_path=self.test_db)
        self.assertTrue(updated)
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "approved")

        # Student leaves group -> becomes available again
        await db.set_member_status(chat_id, user_id, "left", self.test_db)
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "available")

        # Re-request -> pending again
        req_id_2 = await db.record_join_request(chat_id, user_id, course["id"], "pending", self.test_db)
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "pending")

        # Admin rejects -> available for future request
        await db.update_join_request_status(req_id_2, "rejected", processed_by=111, db_path=self.test_db)
        status = await db.get_user_course_status(chat_id, user_id, self.test_db)
        self.assertEqual(status, "available")


if __name__ == "__main__":
    unittest.main()
