import os
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes

# Initialize indexes
init_indexes()

client = TestClient(app)

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    print("test_health passed!")

def test_employees_crud():
    # 1. Create valid employee
    emp_payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    # Clean up if exists
    db.employees.delete_one({"emp_code": "EMP9999"})
    
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert isinstance(data["created_at"], int)
    
    # 2. Duplicate create -> 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # 3. Invalid shift start == shift end -> 422
    invalid_shift = emp_payload.copy()
    invalid_shift["emp_code"] = "EMP9998"
    invalid_shift["shift_end"] = "09:30"
    res_inv = client.post("/employees", json=invalid_shift)
    assert res_inv.status_code == 422

    # 4. List employees
    res_list = client.get("/employees?department=Engineering&page=1&page_size=5")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert "items" in list_data
    assert "total" in list_data
    assert list_data["page"] == 1
    # Check sorted by emp_code asc
    emp_codes = [it["emp_code"] for it in list_data["items"]]
    assert emp_codes == sorted(emp_codes)

    print("test_employees_crud passed!")

def test_punch_in_and_out():
    emp_code = "EMP9999"
    # Ensure fresh state
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Punch in at 09:28 IST (03:58 UTC) on 2026-07-02 (epoch ms: 1782964680000)
    # 2026-07-02T03:58:00Z -> 1782964680000
    p_in_ms = 1782964680000
    res_in = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": p_in_ms, "status": "PRESENT"})
    assert res_in.status_code == 201, res_in.text
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-02"
    assert in_data["punch_in"] == p_in_ms
    assert in_data["punch_out"] is None
    assert in_data["late_minutes"] == 0
    assert "id" not in in_data

    # Duplicate punch-in for same date -> 409
    res_in_dup = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": p_in_ms})
    assert res_in_dup.status_code == 409

    # Punch out at 18:35 IST (13:05 UTC) -> 1782997500000
    p_out_ms = 1782997500000
    res_out = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": p_out_ms})
    assert res_out.status_code == 200, res_out.text
    out_data = res_out.json()
    assert out_data["punch_out"] == p_out_ms
    assert out_data["work_hours"] == 9.12
    assert out_data["overtime_minutes"] == 0
    assert out_data["half_day"] is False

    # Second punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": p_out_ms})
    assert res_out_dup.status_code == 409

    print("test_punch_in_and_out passed!")

def test_regularize():
    emp_code = "EMP9999"
    date_str = "2026-07-02"

    # Regularize punch_in to 10:05 IST (04:35 UTC: 1782966900000)
    new_in = 1782966900000
    patch_payload = {
        "punch_in": new_in,
        "reason": "Traffic jam on highway",
        "regularized_by": "manager.bob"
    }
    res_patch = client.patch(f"/attendance/{emp_code}/{date_str}", json=patch_payload)
    assert res_patch.status_code == 200, res_patch.text
    patch_data = res_patch.json()
    assert patch_data["late_minutes"] == 35
    assert len(patch_data["history"]) == 1
    h0 = patch_data["history"][0]
    assert h0["by"] == "manager.bob"
    assert "late_minutes" in h0["changes"]
    assert h0["changes"]["late_minutes"]["to"] == 35

    # Request that changes nothing -> 422
    res_nochange = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "reason": "No changes at all",
        "regularized_by": "manager.bob"
    })
    assert res_nochange.status_code == 422

    print("test_regularize passed!")

def test_analytics_and_explains():
    # 1. Monthly summary for EMP0001
    res_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200, res_m.text
    m_data = res_m.json()
    assert m_data["emp_code"] == "EMP0001"
    assert m_data["working_days"] == 23
    assert m_data["present_days"] >= 0

    # 2. Department summary
    res_ds = client.get("/analytics/departments/summary?month=2026-07")
    assert res_ds.status_code == 200, res_ds.text
    ds_data = res_ds.json()
    assert ds_data["month"] == "2026-07"
    assert len(ds_data["items"]) >= 1

    # 3. Late leaderboard
    res_lb = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res_lb.status_code == 200, res_lb.text
    lb_data = res_lb.json()
    assert "items" in lb_data
    for item in lb_data["items"]:
        assert item["rank"] <= 5
        assert item["total_late_minutes"] > 0

    # 4. Department trend
    res_tr = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
    assert res_tr.status_code == 200, res_tr.text
    tr_data = res_tr.json()
    assert tr_data["department"] == "Engineering"
    assert len(tr_data["items"]) == 7

    # 5. Admin explain
    endpoints = [
        ("attendance_list", "/admin/explain/attendance_list?page=1&page_size=20"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07&limit=5"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    ]
    for name, url in endpoints:
        exp_res = client.get(url)
        assert exp_res.status_code == 200, f"Explain failed for {name}: {exp_res.text}"
        data = exp_res.json()
        assert "executionStats" in data or "stages" in data or "queryPlanner" in data, f"No stats/plan in {name}"
        print(f"Explain {name} passed!")

    print("test_analytics_and_explains passed!")

if __name__ == "__main__":
    test_health()
    test_employees_crud()
    test_punch_in_and_out()
    test_regularize()
    test_analytics_and_explains()
    print("\nALL ENDPOINT TESTS PASSED COMPLETELY!")
