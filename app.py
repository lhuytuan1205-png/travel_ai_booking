from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from database import mysql, init_db
from werkzeug.utils import secure_filename
import os
import json
import re
from dotenv import load_dotenv
from google import genai
from google.genai import types
import requests

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "travel_ai_booking_secret_key")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None

print("GEMINI_API_KEY:", "OK" if GEMINI_API_KEY else "CHƯA CÓ")

# Cấu hình upload ảnh tour
UPLOAD_FOLDER = "static/images/tours"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

N8N_BOOKING_WEBHOOK_URL = os.getenv("N8N_BOOKING_WEBHOOK_URL")
N8N_BOOKING_STATUS_WEBHOOK_URL = os.getenv("N8N_BOOKING_STATUS_WEBHOOK_URL")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# Kết nối database MySQL
init_db(app)
# =========================
# TRANG CHỦ
# =========================
@app.route("/")
def home():
    cur = mysql.connection.cursor()

    # Lấy tour nổi bật
    cur.execute("""
        SELECT * FROM tours 
        WHERE status = 'active' 
        ORDER BY id DESC 
        LIMIT 8
    """)
    tours = cur.fetchall()

    # Lấy banner đang active
    cur.execute("""
        SELECT * FROM banners
        WHERE status = 'active'
        ORDER BY id DESC
    """)
    banners = cur.fetchall()

    cur.close()

    return render_template(
        "customer/index.html",
        tours=tours,
        banners=banners
    )
# =========================
# DANH SÁCH TOUR KHÁCH HÀNG
# =========================
@app.route("/tours")
def tours():
    promo_code = request.args.get("promo", "").strip().upper()
    selected_promotion = None
    promo_eligible_tour_ids = []
    keyword = request.args.get("keyword", "")
    destination = request.args.get("destination", "")
    min_price = request.args.get("min_price", "")
    max_price = request.args.get("max_price", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    sort = request.args.get("sort", "")

    query = """
        SELECT *
        FROM tours
        WHERE status = 'active'
    """

    params = []

    if keyword:
        query += " AND name LIKE %s"
        params.append(f"%{keyword}%")

    if destination:
        query += " AND destination LIKE %s"
        params.append(f"%{destination}%")

    if min_price:
        query += " AND price >= %s"
        params.append(min_price)

    if max_price:
        query += " AND price <= %s"
        params.append(max_price)

    if start_date:
        query += " AND departure_date >= %s"
        params.append(start_date)

    if end_date:
        query += " AND departure_date <= %s"
        params.append(end_date)

    # Sắp xếp
    if sort == "price_asc":
        query += " ORDER BY price ASC"
    elif sort == "price_desc":
        query += " ORDER BY price DESC"
    elif sort == "date_asc":
        query += " ORDER BY departure_date ASC"
    elif sort == "date_desc":
        query += " ORDER BY departure_date DESC"
    else:
        query += " ORDER BY id DESC"

    cur = mysql.connection.cursor()
    cur.execute(query, tuple(params))
    tours = cur.fetchall()
    if promo_code:
    cur.execute("""
        SELECT
            id,
            title,
            description,
            discount_code,
            discount_value,
            discount_type,
            discount_amount,
            start_date,
            end_date,
            status,
            min_order,
            min_people,
            apply_destination
        FROM promotions
        WHERE UPPER(discount_code) = %s
        AND status = 'active'
        AND (start_date IS NULL OR start_date <= CURDATE())
        AND (end_date IS NULL OR end_date >= CURDATE())
    """, (promo_code,))

    selected_promotion = cur.fetchone()

    if selected_promotion:
        min_order = float(selected_promotion[10] or 0)
        apply_destination = selected_promotion[12]

        for tour in tours:
            tour_id = tour[0]
            tour_destination = tour[2]
            tour_price = float(tour[5] or 0)

            enough_price = tour_price >= min_order

            if apply_destination:
                match_destination = apply_destination.lower() in tour_destination.lower()
            else:
                match_destination = True

            if enough_price and match_destination:
                promo_eligible_tour_ids.append(tour_id)
    cur.close()

    return render_template(
        "customer/tours.html",
        promo_code=promo_code,
        selected_promotion=selected_promotion,
        promo_eligible_tour_ids=promo_eligible_tour_ids
        tours=tours,
        keyword=keyword,
        destination=destination,
        min_price=min_price,
        max_price=max_price,
        start_date=start_date,
        end_date=end_date,
        sort=sort
    )
# =========================
# CHI TIẾT TOUR
# =========================
@app.route("/tour/<int:tour_id>")
def tour_detail(tour_id):
    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM tours WHERE id = %s AND status = 'active'", (tour_id,))
    tour = cur.fetchone()

    if not tour:
        cur.close()
        flash("Tour không tồn tại hoặc đã ngừng bán!")
        return redirect(url_for("tours"))

    cur.execute("SELECT * FROM tour_images WHERE tour_id = %s ORDER BY id DESC", (tour_id,))
    tour_images = cur.fetchall()

    cur.close()

    return render_template("customer/tour_detail.html", tour=tour, tour_images=tour_images)
# =========================
# ĐẶT TOUR
# =========================
def send_booking_email_to_n8n(booking_data):
    try:
        response = requests.post(
            N8N_BOOKING_WEBHOOK_URL,
            json=booking_data,
            timeout=15
        )

        print("N8N status:", response.status_code)
        print("N8N response:", response.text)

        return response.status_code in [200, 201]

    except Exception as e:
        print("Lỗi gửi dữ liệu sang n8n:", e)
        return False
@app.route("/booking/<int:tour_id>", methods=["GET", "POST"])
def booking(tour_id):
    # Bắt buộc đăng nhập mới được đặt tour
    if "user_id" not in session:
        flash("Bạn cần đăng nhập trước khi đặt tour!")
        return redirect(url_for("login", next=f"/booking/{tour_id}"))

    cur = mysql.connection.cursor()

    # Lấy thông tin tour
    cur.execute("SELECT * FROM tours WHERE id = %s AND status = 'active'", (tour_id,))
    tour = cur.fetchone()

    if not tour:
        cur.close()
        flash("Tour không tồn tại hoặc đã ngừng bán!")
        return redirect(url_for("tours"))

    if request.method == "POST":
        full_name = request.form["full_name"]
        email = request.form["email"]
        phone = request.form["phone"]
        number_of_people = int(request.form["number_of_people"])
        note = request.form.get("note", "")

        original_price = float(tour[5]) * number_of_people
        discount_amount = 0
        total_price = original_price
        promotion_code = request.form.get("promotion_code", "").strip().upper()

        # Kiểm tra mã khuyến mãi nếu khách có nhập
        if promotion_code:
            cur.execute("""
                SELECT discount_code, discount_type, discount_amount
                FROM promotions
                WHERE UPPER(discount_code) = %s
                AND status = 'active'
                AND (start_date IS NULL OR start_date <= CURDATE())
                AND (end_date IS NULL OR end_date >= CURDATE())
            """, (promotion_code,))

            promotion = cur.fetchone()

            if promotion:
                discount_type = promotion[1]
                discount_value = float(promotion[2] or 0)

                if discount_value <= 0:
                    flash("Mã khuyến mãi chưa được cấu hình giá trị giảm!")
                    cur.close()
                    return redirect(url_for("booking", tour_id=tour_id))

                if discount_type == "percent":
                    discount_amount = original_price * discount_value / 100
                elif discount_type == "amount":
                    discount_amount = discount_value

                # Không cho giảm quá tổng tiền
                if discount_amount > original_price:
                    discount_amount = original_price

                total_price = original_price - discount_amount
            else:
                flash("Mã khuyến mãi không hợp lệ hoặc đã hết hạn!")
                cur.close()
                return redirect(url_for("booking", tour_id=tour_id))

        cur.execute("""
            INSERT INTO bookings
            (
                user_id,
                tour_id,
                full_name,
                email,
                phone,
                number_of_people,
                note,
                original_price,
                discount_amount,
                promotion_code,
                total_price,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            session["user_id"],
            tour_id,
            full_name,
            email,
            phone,
            number_of_people,
            note,
            original_price,
            discount_amount,
            promotion_code if promotion_code else None,
            total_price,
            "pending"
        ))

        mysql.connection.commit()
        booking_id = cur.lastrowid

        booking_data = {
            "booking_id": booking_id,
            "customer_name": full_name,
            "customer_email": email,
            "phone": phone,
            "tour_name": tour[1],
            "destination": tour[2],
            "duration": tour[6],
            "departure_date": str(tour[7]),
            "number_of_people": number_of_people,
            "original_price": original_price,
            "discount_amount": discount_amount,
            "promotion_code": promotion_code if promotion_code else "",
            "total_price": total_price,
            "status": "pending"
        }

        send_booking_email_to_n8n(booking_data)

        cur.close()

        return redirect(url_for("booking_success", booking_id=booking_id))
    cur.close()

    return render_template("customer/booking.html", tour=tour)
# =========================
# ĐẶT TOUR THÀNH CÔNG
# =========================
@app.route("/booking-success/<int:booking_id>")
def booking_success(booking_id):
    if "user_id" not in session:
        flash("Vui lòng đăng nhập!")
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.note,
            CAST(bookings.original_price AS DECIMAL(12,2)),
            CAST(bookings.discount_amount AS DECIMAL(12,2)),
            bookings.promotion_code,
            CAST(bookings.total_price AS DECIMAL(12,2)),
            bookings.status,
            bookings.created_at,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date,
            tours.image
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        WHERE bookings.id = %s AND bookings.user_id = %s
    """, (booking_id, session["user_id"]))

    booking = cur.fetchone()
    cur.close()

    if not booking:
        flash("Không tìm thấy thông tin đặt tour!")
        return redirect(url_for("tours"))

    return render_template("customer/booking_success.html", booking=booking)
# =========================
# ĐĂNG KÝ TÀI KHOẢN
# =========================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form["full_name"]
        email = request.form["email"]
        phone = request.form["phone"]
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        # Kiểm tra nhập lại mật khẩu
        if password != confirm_password:
            flash("Mật khẩu nhập lại không khớp!")
            return redirect(url_for("register"))

        cur = mysql.connection.cursor()

        # Kiểm tra email đã tồn tại chưa
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        existing_user = cur.fetchone()

        if existing_user:
            cur.close()
            flash("Email này đã được đăng ký!")
            return redirect(url_for("register"))

        # Mã hóa mật khẩu
        hashed_password = generate_password_hash(password)

        # Thêm user mới, mặc định là customer
        cur.execute("""
            INSERT INTO users (full_name, email, phone, password, role)
            VALUES (%s, %s, %s, %s, %s)
        """, (full_name, email, phone, hashed_password, "customer"))

        mysql.connection.commit()
        cur.close()

        flash("Đăng ký thành công! Vui lòng đăng nhập.")
        return redirect(url_for("login"))

    return render_template("customer/register.html")


# =========================
# ĐĂNG NHẬP
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close()

        if user and check_password_hash(user[4], password):
            session["user_id"] = user[0]
            session["full_name"] = user[1]
            session["email"] = user[2]
            session["role"] = user[5]

            flash("Đăng nhập thành công!")

            next_url = request.args.get("next")

            if next_url:
                return redirect(next_url)

            if user[5] == "admin":
                return redirect(url_for("admin_dashboard_page"))

            return redirect(url_for("home"))

    return render_template("customer/login.html")


# =========================
# ĐĂNG XUẤT
# =========================
@app.route("/logout")
def logout():
    session.clear()
    flash("Bạn đã đăng xuất.")
    return redirect(url_for("home"))


# =========================
# TRANG ADMIN
# =========================
@app.route("/admin")
def admin_dashboard():
    if not is_admin():
        return redirect(url_for("login"))

    return redirect(url_for("admin_dashboard_page"))

# =========================
# TẠO ADMIN MẪU
# =========================
@app.route("/create-admin")
def create_admin():
    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM users WHERE email = %s", ("admin@gmail.com",))
    admin = cur.fetchone()

    if admin:
        cur.close()
        return "Tài khoản admin đã tồn tại!"

    hashed_password = generate_password_hash("123456")

    cur.execute("""
        INSERT INTO users (full_name, email, phone, password, role)
        VALUES (%s, %s, %s, %s, %s)
    """, ("Quản trị viên", "admin@gmail.com", "0123456789", hashed_password, "admin"))

    mysql.connection.commit()
    cur.close()

    return "Tạo tài khoản admin thành công! Email: admin@gmail.com | Mật khẩu: 123456"
# =========================
# LỊCH SỬ ĐẶT TOUR CỦA KHÁCH HÀNG
# =========================
@app.route("/my-bookings")
def my_bookings():
    # Bắt buộc đăng nhập
    if "user_id" not in session:
        flash("Vui lòng đăng nhập để xem lịch sử đặt tour!")
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT 
            bookings.id,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date,
            bookings.number_of_people,
            bookings.total_price,
            bookings.status,
            bookings.created_at
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        WHERE bookings.user_id = %s
        ORDER BY bookings.id DESC
    """, (session["user_id"],))

    bookings = cur.fetchall()
    cur.close()

    return render_template("customer/my_bookings.html", bookings=bookings)
# =========================
# HÀM KIỂM TRA QUYỀN ADMIN
# =========================
def is_admin():
    if "user_id" not in session:
        flash("Vui lòng đăng nhập trước!")
        return False

    if session.get("role") != "admin":
        flash("Bạn không có quyền truy cập trang admin!")
        return False

    return True


# =========================
# ADMIN DASHBOARD
# =========================
@app.route("/admin/dashboard")
def admin_dashboard_page():
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    # Thống kê tổng quan
    cur.execute("SELECT COUNT(*) FROM tours")
    total_tours = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM bookings")
    total_bookings = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE role = 'customer'")
    total_customers = cur.fetchone()[0]

    cur.execute("SELECT SUM(total_price) FROM bookings WHERE status != 'cancelled'")
    total_revenue = cur.fetchone()[0] or 0
    

    # Đơn mới nhất
    cur.execute("""
        SELECT 
            bookings.id,
            bookings.full_name,
            bookings.phone,
            bookings.number_of_people,
            bookings.total_price,
            bookings.status,
            bookings.created_at,
            tours.name
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        ORDER BY
            CASE
                WHEN bookings.status = 'pending' THEN 1
                WHEN bookings.status = 'confirmed' THEN 2
                WHEN bookings.status = 'cancelled' THEN 3
                ELSE 4
            END ASC,
            CASE
                WHEN bookings.status = 'pending' THEN bookings.created_at
            END ASC,
            bookings.id DESC
        LIMIT 5
    """)
    latest_bookings = cur.fetchall()

    # Tour mới nhất
    cur.execute("""
        SELECT id, name, destination, price, status, created_at
        FROM tours
        ORDER BY id DESC
        LIMIT 5
    """)

    latest_tours = cur.fetchall()

    cur.close()

    return render_template(
        "admin/dashboard.html",
        total_tours=total_tours,
        total_bookings=total_bookings,
        total_customers=total_customers,
        total_revenue=total_revenue,
        latest_bookings=latest_bookings,
        latest_tours=latest_tours
    )

# =========================
# ADMIN - DANH SÁCH TOUR
# =========================
@app.route("/admin/tours")
def admin_manage_tours():
    if not is_admin():
        return redirect(url_for("login"))

    keyword = request.args.get("keyword", "")
    destination = request.args.get("destination", "")
    status = request.args.get("status", "")

    page = request.args.get("page", 1, type=int)
    per_page = 5
    offset = (page - 1) * per_page

    base_query = "FROM tours WHERE 1=1"
    params = []

    if keyword:
        base_query += " AND name LIKE %s"
        params.append(f"%{keyword}%")

    if destination:
        base_query += " AND destination LIKE %s"
        params.append(f"%{destination}%")

    if status:
        base_query += " AND status = %s"
        params.append(status)

    cur = mysql.connection.cursor()

    # Đếm tổng số tour sau khi lọc
    count_query = "SELECT COUNT(*) " + base_query
    cur.execute(count_query, tuple(params))
    total_items = cur.fetchone()[0]

    total_pages = (total_items + per_page - 1) // per_page

    # Lấy dữ liệu theo trang
    data_query = "SELECT * " + base_query + " ORDER BY id DESC LIMIT %s OFFSET %s"
    cur.execute(data_query, tuple(params + [per_page, offset]))
    tours = cur.fetchall()

    cur.execute("SELECT DISTINCT destination FROM tours ORDER BY destination ASC")
    destinations = cur.fetchall()

    cur.close()

    return render_template(
        "admin/manage_tours.html",
        tours=tours,
        destinations=destinations,
        keyword=keyword,
        destination=destination,
        status=status,
        page=page,
        total_pages=total_pages
    )

# =========================
# ADMIN - THÊM TOUR
# =========================
@app.route("/admin/tours/add", methods=["GET", "POST"])
def admin_add_tour():
    if not is_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form["name"]
        destination = request.form["destination"]
        description = request.form["description"]
        schedule = request.form["schedule"]
        price = request.form["price"]
        duration = request.form["duration"]
        departure_date = request.form["departure_date"]
        status = request.form["status"]

        image_file = request.files.get("image")

        if not image_file or image_file.filename == "":
            flash("Vui lòng chọn ảnh tour!")
            return redirect(url_for("admin_add_tour"))

        if not allowed_file(image_file.filename):
            flash("File ảnh không hợp lệ! Chỉ chấp nhận png, jpg, jpeg, webp.")
            return redirect(url_for("admin_add_tour"))

        filename = secure_filename(image_file.filename)

        # Đổi tên ảnh để tránh trùng tên
        filename = f"{name.lower().replace(' ', '-')}-{filename}"
        filename = secure_filename(filename)

        image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        image_file.save(image_path)

        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO tours
            (name, destination, description, schedule, price, duration, departure_date, image, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            name,
            destination,
            description,
            schedule,
            price,
            duration,
            departure_date,
            filename,
            status
        ))
        mysql.connection.commit()

        # Lấy id tour vừa thêm
        tour_id = cur.lastrowid

        # Upload nhiều ảnh phụ
        gallery_files = request.files.getlist("gallery_images")

        for file in gallery_files:
            if file and file.filename != "":
                if allowed_file(file.filename):
                    gallery_filename = secure_filename(file.filename)
                    gallery_filename = f"{tour_id}-gallery-{gallery_filename}"
                    gallery_filename = secure_filename(gallery_filename)

                    gallery_path = os.path.join(app.config["UPLOAD_FOLDER"], gallery_filename)
                    file.save(gallery_path)

                    cur.execute("""
                        INSERT INTO tour_images (tour_id, image)
                        VALUES (%s, %s)
                        """, (tour_id, gallery_filename))

        mysql.connection.commit()
        cur.close()

        flash("Thêm tour và upload ảnh thành công!")
        return redirect(url_for("admin_manage_tours"))

    return render_template("admin/add_tour.html")
# =========================
# ADMIN - SỬA TOUR
# =========================
@app.route("/admin/tours/edit/<int:tour_id>", methods=["GET", "POST"])
def admin_edit_tour(tour_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    # Lấy thông tin tour hiện tại
    cur.execute("SELECT * FROM tours WHERE id = %s", (tour_id,))
    tour = cur.fetchone()

    if not tour:
        cur.close()
        flash("Không tìm thấy tour!")
        return redirect(url_for("admin_manage_tours"))

    if request.method == "POST":
        name = request.form["name"]
        destination = request.form["destination"]
        description = request.form["description"]
        schedule = request.form["schedule"]
        price = request.form["price"]
        duration = request.form["duration"]
        departure_date = request.form["departure_date"]
        status = request.form["status"]

        # Giữ ảnh đại diện cũ nếu admin không chọn ảnh mới
        filename = tour[8]

        # Upload ảnh đại diện mới nếu có
        image_file = request.files.get("image")

        if image_file and image_file.filename != "":
            if not allowed_file(image_file.filename):
                cur.close()
                flash("File ảnh đại diện không hợp lệ!")
                return redirect(url_for("admin_edit_tour", tour_id=tour_id))

            filename = secure_filename(image_file.filename)
            filename = f"{tour_id}-main-{filename}"
            filename = secure_filename(filename)

            image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            image_file.save(image_path)

        # Cập nhật thông tin tour
        cur.execute("""
            UPDATE tours
            SET name = %s,
                destination = %s,
                description = %s,
                schedule = %s,
                price = %s,
                duration = %s,
                departure_date = %s,
                image = %s,
                status = %s
            WHERE id = %s
        """, (
            name,
            destination,
            description,
            schedule,
            price,
            duration,
            departure_date,
            filename,
            status,
            tour_id
        ))

        # Upload thêm từng ảnh chi tiết mới
        gallery_files = request.files.getlist("gallery_images")

        for file in gallery_files:
            if file and file.filename != "":
                if allowed_file(file.filename):
                    gallery_filename = secure_filename(file.filename)
                    gallery_filename = f"{tour_id}-gallery-{gallery_filename}"
                    gallery_filename = secure_filename(gallery_filename)

                    gallery_path = os.path.join(app.config["UPLOAD_FOLDER"], gallery_filename)
                    file.save(gallery_path)

                    cur.execute("""
                        INSERT INTO tour_images (tour_id, image)
                        VALUES (%s, %s)
                    """, (tour_id, gallery_filename))

        mysql.connection.commit()
        cur.close()

        flash("Cập nhật tour thành công!")
        return redirect(url_for("admin_edit_tour", tour_id=tour_id))

    # Nếu là GET thì lấy danh sách ảnh chi tiết hiện có
    cur.execute("""
        SELECT * FROM tour_images
        WHERE tour_id = %s
        ORDER BY id DESC
    """, (tour_id,))

    tour_images = cur.fetchall()

    cur.execute("""
        SELECT * FROM tour_images
        WHERE tour_id = %s
        ORDER BY id DESC
    """, (tour_id,))

    tour_images = cur.fetchall()

    cur.close()

    return render_template(
        "admin/edit_tour.html",
        tour=tour,
        tour_images=tour_images
    )
# =========================
# ADMIN - XÓA TOUR
# =========================
@app.route("/admin/tours/delete/<int:tour_id>")
def admin_delete_tour(tour_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    # Kiểm tra tour đã có đơn đặt chưa
    cur.execute("SELECT COUNT(*) FROM bookings WHERE tour_id = %s", (tour_id,))
    booking_count = cur.fetchone()[0]

    if booking_count > 0:
        # Không xóa cứng nếu đã có đơn, chỉ ẩn tour
        cur.execute("UPDATE tours SET status = 'inactive' WHERE id = %s", (tour_id,))
        mysql.connection.commit()
        cur.close()

        flash("Tour đã có đơn đặt nên hệ thống chỉ chuyển sang trạng thái ẩn!")
        return redirect(url_for("admin_manage_tours"))

    cur.execute("DELETE FROM tours WHERE id = %s", (tour_id,))
    mysql.connection.commit()
    cur.close()

    flash("Xóa tour thành công!")
    return redirect(url_for("admin_manage_tours"))
# =========================
# ADMIN - QUẢN LÝ ĐƠN ĐẶT TOUR
# =========================
@app.route("/admin/bookings")
def admin_manage_bookings():
    if not is_admin():
        return redirect(url_for("login"))

    keyword = request.args.get("keyword", "")
    status = request.args.get("status", "")

    page = request.args.get("page", 1, type=int)
    per_page = 5
    offset = (page - 1) * per_page

    base_query = """
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        JOIN users ON bookings.user_id = users.id
        WHERE 1=1
    """

    params = []

    if keyword:
        base_query += """
            AND (
                bookings.full_name LIKE %s
                OR bookings.email LIKE %s
                OR bookings.phone LIKE %s
                OR tours.name LIKE %s
            )
        """
        search_value = f"%{keyword}%"
        params.extend([search_value, search_value, search_value, search_value])

    if status:
        base_query += " AND bookings.status = %s"
        params.append(status)

    cur = mysql.connection.cursor()

    count_query = "SELECT COUNT(*) " + base_query
    cur.execute(count_query, tuple(params))
    total_items = cur.fetchone()[0]

    total_pages = (total_items + per_page - 1) // per_page

    data_query = """
        SELECT 
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.total_price,
            bookings.status,
            bookings.created_at,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date,
            users.full_name
    """ + base_query + """
        ORDER BY
            CASE
                WHEN bookings.status = 'pending' THEN 1
                WHEN bookings.status = 'confirmed' THEN 2
                WHEN bookings.status = 'cancelled' THEN 3
                ELSE 4
            END ASC,
            CASE
                WHEN bookings.status = 'pending' THEN bookings.created_at
            END ASC,
            bookings.id DESC
        LIMIT %s OFFSET %s
    """

    cur.execute(data_query, tuple(params + [per_page, offset]))
    bookings = cur.fetchall()

    cur.close()

    return render_template(
        "admin/manage_bookings.html",
        bookings=bookings,
        keyword=keyword,
        status=status,
        page=page,
        total_pages=total_pages
    )
# =========================
# ADMIN - XÁC NHẬN ĐƠN
# =========================
def send_booking_status_email_to_n8n(booking_data):
    try:
        response = requests.post(
            N8N_BOOKING_STATUS_WEBHOOK_URL,
            json=booking_data,
            timeout=15
        )

        print("N8N status email:", response.status_code)
        print("N8N status response:", response.text)

        return response.status_code in [200, 201]

    except Exception as e:
        print("Lỗi gửi email trạng thái đơn sang n8n:", e)
        return False
@app.route("/admin/bookings/confirm/<int:booking_id>")
def admin_confirm_booking(booking_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.total_price,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        WHERE bookings.id = %s
    """, (booking_id,))

    booking = cur.fetchone()

    if not booking:
        cur.close()
        flash("Không tìm thấy đơn đặt tour!")
        return redirect(url_for("admin_manage_bookings"))

    cur.execute("""
        UPDATE bookings
        SET status = 'confirmed'
        WHERE id = %s
    """, (booking_id,))

    mysql.connection.commit()

    booking_data = {
        "booking_id": booking[0],
        "customer_name": booking[1],
        "customer_email": booking[2],
        "phone": booking[3],
        "number_of_people": booking[4],
        "total_price": float(booking[5] or 0),
        "tour_name": booking[6],
        "destination": booking[7],
        "duration": booking[8],
        "departure_date": str(booking[9]),
        "status": "confirmed"
    }

    send_booking_status_email_to_n8n(booking_data)

    cur.close()

    flash("Đã xác nhận đơn đặt tour và gửi email thông báo cho khách!")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))
# =========================
# ADMIN - HỦY ĐƠN
# =========================
def send_booking_status_email_to_n8n(booking_data):
    try:
        response = requests.post(
            N8N_BOOKING_STATUS_WEBHOOK_URL,
            json=booking_data,
            timeout=15
        )

        print("N8N status email:", response.status_code)
        print("N8N status response:", response.text)

        return response.status_code in [200, 201]

    except Exception as e:
        print("Lỗi gửi email trạng thái đơn sang n8n:", e)
        return False
@app.route("/admin/bookings/cancel/<int:booking_id>")
def admin_cancel_booking(booking_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.total_price,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        WHERE bookings.id = %s
    """, (booking_id,))

    booking = cur.fetchone()

    if not booking:
        cur.close()
        flash("Không tìm thấy đơn đặt tour!")
        return redirect(url_for("admin_manage_bookings"))

    cur.execute("""
        UPDATE bookings
        SET status = 'cancelled'
        WHERE id = %s
    """, (booking_id,))

    mysql.connection.commit()

    booking_data = {
        "booking_id": booking[0],
        "customer_name": booking[1],
        "customer_email": booking[2],
        "phone": booking[3],
        "number_of_people": booking[4],
        "total_price": float(booking[5] or 0),
        "tour_name": booking[6],
        "destination": booking[7],
        "duration": booking[8],
        "departure_date": str(booking[9]),
        "status": "cancelled"
    }

    send_booking_status_email_to_n8n(booking_data)

    cur.close()

    flash("Đã hủy đơn đặt tour và gửi email thông báo cho khách!")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))
# =========================
# ADMIN - QUẢN LÝ NGƯỜI DÙNG
# =========================
@app.route("/admin/users")
def admin_manage_users():
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT 
            id,
            full_name,
            email,
            phone,
            role,
            created_at
        FROM users
        ORDER BY id DESC
    """)

    users = cur.fetchall()
    cur.close()

    return render_template("admin/manage_users.html", users=users)
# =========================
# ADMIN - ĐỔI QUYỀN NGƯỜI DÙNG
# =========================
@app.route("/admin/users/change-role/<int:user_id>/<string:new_role>")
def admin_change_user_role(user_id, new_role):
    if not is_admin():
        return redirect(url_for("login"))

    if new_role not in ["customer", "admin"]:
        flash("Quyền không hợp lệ!")
        return redirect(url_for("admin_manage_users"))

    # Không cho admin tự hạ quyền chính mình
    if user_id == session.get("user_id") and new_role == "customer":
        flash("Bạn không thể tự hạ quyền tài khoản admin đang đăng nhập!")
        return redirect(url_for("admin_manage_users"))

    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()

    if not user:
        cur.close()
        flash("Không tìm thấy người dùng!")
        return redirect(url_for("admin_manage_users"))

    cur.execute("""
        UPDATE users
        SET role = %s
        WHERE id = %s
    """, (new_role, user_id))

    mysql.connection.commit()
    cur.close()

    flash("Cập nhật quyền người dùng thành công!")
    return redirect(url_for("admin_manage_users"))
# =========================
# AI TƯ VẤN TOUR - GỢI Ý TỪ MYSQL
# =========================
@app.route("/ai-consultant", methods=["GET", "POST"])
def ai_consultant():
    suggested_tours = []
    ai_message = ""
    budget = ""
    destination = ""
    duration = ""
    interest = ""

    if request.method == "POST":
        budget = request.form.get("budget", "").strip()
        destination = request.form.get("destination", "").strip()
        duration = request.form.get("duration", "").strip()
        interest = request.form.get("interest", "").strip()

        query = """
            SELECT *
            FROM tours
            WHERE status = 'active'
        """

        params = []

        # Lọc theo ngân sách
        if budget:
            query += " AND price <= %s"
            params.append(budget)

        # Lọc theo điểm đến
        if destination:
            query += " AND destination LIKE %s"
            params.append(f"%{destination}%")

        # Lọc theo thời gian tour
        if duration:
            query += " AND duration LIKE %s"
            params.append(f"%{duration}%")

        # Lọc theo sở thích trong tên, mô tả, lịch trình
        if interest:
            query += """
                AND (
                    name LIKE %s
                    OR description LIKE %s
                    OR schedule LIKE %s
                    OR destination LIKE %s
                )
            """
            interest_value = f"%{interest}%"
            params.extend([interest_value, interest_value, interest_value, interest_value])

        query += " ORDER BY price ASC LIMIT 6"

        cur = mysql.connection.cursor()
        cur.execute(query, tuple(params))
        suggested_tours = cur.fetchall()
        cur.close()

        if suggested_tours:
            ai_message = "Dựa trên thông tin bạn cung cấp, hệ thống đã tìm thấy một số tour phù hợp với ngân sách, thời gian và sở thích của bạn."
        else:
            ai_message = "Hiện chưa tìm thấy tour thật sự phù hợp. Bạn có thể thử tăng ngân sách, đổi điểm đến hoặc bỏ bớt điều kiện lọc."

    return render_template(
        "customer/ai_consultant.html",
        suggested_tours=suggested_tours,
        ai_message=ai_message,
        budget=budget,
        destination=destination,
        duration=duration,
        interest=interest
    )
# =========================
# KHÁCH SẠN
# =========================
@app.route("/hotels")
def hotels():
    return render_template("customer/hotels.html")


# =========================
# COMBO DU LỊCH
# =========================
@app.route("/combos")
def combos():
    return render_template("customer/combos.html")


# =========================
# KHUYẾN MÃI
# =========================
@app.route("/promotions")
def promotions():
    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT *
        FROM promotions
        WHERE status = 'active'
        ORDER BY id DESC
    """)

    promotions = cur.fetchall()
    cur.close()

    return render_template("customer/promotions.html", promotions=promotions)
# =========================
# ADMIN - QUẢN LÝ KHUYẾN MÃI
# =========================
@app.route("/admin/promotions")
def admin_manage_promotions():
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM promotions ORDER BY id DESC")
    promotions = cur.fetchall()
    cur.close()

    return render_template("admin/manage_promotions.html", promotions=promotions)
# =========================
# ADMIN - THÊM KHUYẾN MÃI
# =========================
@app.route("/admin/promotions/add", methods=["GET", "POST"])
def admin_add_promotion():
    if not is_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form["title"]
        description = request.form.get("description", "")
        discount_code = request.form["discount_code"].strip().upper()
        discount_value = request.form["discount_value"]
        discount_type = request.form["discount_type"]
        discount_amount = request.form["discount_amount"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        status = request.form["status"]

        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO promotions
            (
                title,
                description,
                discount_code,
                discount_value,
                discount_type,
                discount_amount,
                start_date,
                end_date,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            title,
            description,
            discount_code,
            discount_value,
            discount_type,
            discount_amount,
            start_date,
            end_date,
            status
        ))

        mysql.connection.commit()
        cur.close()

        flash("Thêm khuyến mãi thành công!")
        return redirect(url_for("admin_manage_promotions"))

    return render_template("admin/add_promotion.html")
# =========================
# ADMIN - SỬA KHUYẾN MÃI
# =========================
@app.route("/admin/promotions/edit/<int:promotion_id>", methods=["GET", "POST"])
def admin_edit_promotion(promotion_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM promotions WHERE id = %s", (promotion_id,))
    promotion = cur.fetchone()

    if not promotion:
        cur.close()
        flash("Không tìm thấy khuyến mãi!")
        return redirect(url_for("admin_manage_promotions"))

    if request.method == "POST":
        title = request.form["title"]
        description = request.form["description"]
        discount_code = request.form["discount_code"]
        discount_value = request.form["discount_value"]
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]
        status = request.form["status"]

        cur.execute("""
            UPDATE promotions
            SET title = %s,
                description = %s,
                discount_code = %s,
                discount_value = %s,
                start_date = %s,
                end_date = %s,
                status = %s
            WHERE id = %s
        """, (
            title,
            description,
            discount_code,
            discount_value,
            start_date,
            end_date,
            status,
            promotion_id
        ))

        mysql.connection.commit()
        cur.close()

        flash("Cập nhật khuyến mãi thành công!")
        return redirect(url_for("admin_manage_promotions"))

    cur.close()

    return render_template("admin/edit_promotion.html", promotion=promotion)
# =========================
# ADMIN - XÓA KHUYẾN MÃI
# =========================
@app.route("/admin/promotions/delete/<int:promotion_id>")
def admin_delete_promotion(promotion_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM promotions WHERE id = %s", (promotion_id,))
    mysql.connection.commit()
    cur.close()

    flash("Xóa khuyến mãi thành công!")
    return redirect(url_for("admin_manage_promotions"))

# =========================
# LIÊN HỆ
# =========================
@app.route("/contact")
def contact():
    return render_template("customer/contact.html")
# =========================
# THÔNG TIN CÁ NHÂN KHÁCH HÀNG
# =========================
@app.route("/profile")
def profile():
    if "user_id" not in session:
        flash("Vui lòng đăng nhập để xem thông tin cá nhân!")
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT id, full_name, email, phone, role, created_at
        FROM users
        WHERE id = %s
    """, (session["user_id"],))

    user = cur.fetchone()
    cur.close()

    if not user:
        flash("Không tìm thấy tài khoản!")
        return redirect(url_for("home"))

    return render_template("customer/profile.html", user=user)
# =========================
# ADMIN - XÓA ẢNH CHI TIẾT TOUR
# =========================
@app.route("/admin/tour-images/delete/<int:image_id>/<int:tour_id>")
def admin_delete_tour_image(image_id, tour_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    # Lấy tên file ảnh trong database
    cur.execute("SELECT image FROM tour_images WHERE id = %s", (image_id,))
    image = cur.fetchone()

    if not image:
        cur.close()
        flash("Không tìm thấy ảnh cần xóa!")
        return redirect(url_for("admin_edit_tour", tour_id=tour_id))

    image_name = image[0]
    image_path = os.path.join(app.config["UPLOAD_FOLDER"], image_name)

    # Xóa ảnh trong database
    cur.execute("DELETE FROM tour_images WHERE id = %s", (image_id,))
    mysql.connection.commit()
    cur.close()

    # Xóa file ảnh trong thư mục nếu tồn tại
    if os.path.exists(image_path):
        os.remove(image_path)

    flash("Đã xóa ảnh chi tiết tour!")
    return redirect(url_for("admin_edit_tour", tour_id=tour_id))
# =========================
# ADMIN - CHI TIẾT ĐƠN ĐẶT TOUR
# =========================
@app.route("/admin/bookings/detail/<int:booking_id>")
def admin_booking_detail(booking_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT 
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.note,
            bookings.total_price,
            bookings.status,
            bookings.created_at,
            tours.id,
            tours.name,
            tours.destination,
            tours.description,
            tours.schedule,
            tours.price,
            tours.duration,
            tours.departure_date,
            tours.image,
            users.full_name,
            users.email
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        JOIN users ON bookings.user_id = users.id
        WHERE bookings.id = %s
    """, (booking_id,))

    booking = cur.fetchone()
    cur.close()

    if not booking:
        flash("Không tìm thấy đơn đặt tour!")
        return redirect(url_for("admin_manage_bookings"))

    return render_template("admin/booking_detail.html", booking=booking)
# =========================
# ADMIN - QUẢN LÝ BANNER
# =========================
@app.route("/admin/banners")
def admin_manage_banners():
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM banners ORDER BY id DESC")
    banners = cur.fetchall()
    cur.close()

    return render_template("admin/manage_banners.html", banners=banners)
# =========================
# ADMIN - THÊM BANNER
# =========================
@app.route("/admin/banners/add", methods=["GET", "POST"])
def admin_add_banner():
    if not is_admin():
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form["title"]
        subtitle = request.form["subtitle"]
        description = request.form["description"]
        status = request.form["status"]

        image_file = request.files.get("image")

        if not image_file or image_file.filename == "":
            flash("Vui lòng chọn ảnh banner!")
            return redirect(url_for("admin_add_banner"))

        if not allowed_file(image_file.filename):
            flash("File ảnh không hợp lệ! Chỉ chấp nhận png, jpg, jpeg, webp.")
            return redirect(url_for("admin_add_banner"))

        filename = secure_filename(image_file.filename)
        filename = f"banner-{filename}"
        filename = secure_filename(filename)

        banner_folder = "static/images/banners"
        image_path = os.path.join(banner_folder, filename)
        image_file.save(image_path)

        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO banners (title, subtitle, description, image, status)
            VALUES (%s, %s, %s, %s, %s)
        """, (title, subtitle, description, filename, status))

        mysql.connection.commit()
        cur.close()

        flash("Thêm banner thành công!")
        return redirect(url_for("admin_manage_banners"))

    return render_template("admin/add_banner.html")
# =========================
# ADMIN - SỬA BANNER
# =========================
@app.route("/admin/banners/edit/<int:banner_id>", methods=["GET", "POST"])
def admin_edit_banner(banner_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()
    cur.execute("SELECT * FROM banners WHERE id = %s", (banner_id,))
    banner = cur.fetchone()

    if not banner:
        cur.close()
        flash("Không tìm thấy banner!")
        return redirect(url_for("admin_manage_banners"))

    if request.method == "POST":
        title = request.form["title"]
        subtitle = request.form["subtitle"]
        description = request.form["description"]
        status = request.form["status"]

        filename = banner[4]

        image_file = request.files.get("image")

        if image_file and image_file.filename != "":
            if not allowed_file(image_file.filename):
                cur.close()
                flash("File ảnh không hợp lệ!")
                return redirect(url_for("admin_edit_banner", banner_id=banner_id))

            filename = secure_filename(image_file.filename)
            filename = f"banner-{banner_id}-{filename}"
            filename = secure_filename(filename)

            banner_folder = "static/images/banners"
            image_path = os.path.join(banner_folder, filename)
            image_file.save(image_path)

        cur.execute("""
            UPDATE banners
            SET title = %s,
                subtitle = %s,
                description = %s,
                image = %s,
                status = %s
            WHERE id = %s
        """, (title, subtitle, description, filename, status, banner_id))

        mysql.connection.commit()
        cur.close()

        flash("Cập nhật banner thành công!")
        return redirect(url_for("admin_manage_banners"))

    cur.close()

    return render_template("admin/edit_banner.html", banner=banner)
# =========================
# ADMIN - XÓA BANNER
# =========================
@app.route("/admin/banners/delete/<int:banner_id>")
def admin_delete_banner(banner_id):
    if not is_admin():
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("SELECT image FROM banners WHERE id = %s", (banner_id,))
    banner = cur.fetchone()

    if not banner:
        cur.close()
        flash("Không tìm thấy banner!")
        return redirect(url_for("admin_manage_banners"))

    image_name = banner[0]
    image_path = os.path.join("static/images/banners", image_name)

    cur.execute("DELETE FROM banners WHERE id = %s", (banner_id,))
    mysql.connection.commit()
    cur.close()

    if os.path.exists(image_path):
        os.remove(image_path)

    flash("Xóa banner thành công!")
    return redirect(url_for("admin_manage_banners"))
# =========================
# KIỂM TRA MÃ KHUYẾN MÃI
# =========================
@app.route("/check-promotion", methods=["POST"])
def check_promotion():
    data = request.get_json()

    promotion_code = data.get("promotion_code", "").strip().upper()
    price_per_person = float(data.get("price_per_person", 0))
    number_of_people = int(data.get("number_of_people", 1))

    if not promotion_code:
        return jsonify({
            "success": False,
            "message": "Vui lòng nhập mã khuyến mãi."
        })

    original_price = price_per_person * number_of_people

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT 
            discount_code,
            discount_type,
            discount_amount
        FROM promotions
        WHERE UPPER(discount_code) = %s
        AND status = 'active'
        AND start_date <= CURDATE()
        AND end_date >= CURDATE()
    """, (promotion_code,))

    promotion = cur.fetchone()
    cur.close()

    if not promotion:
        return jsonify({
            "success": False,
            "message": "Mã khuyến mãi không hợp lệ hoặc đã hết hạn."
        })

    discount_type = promotion[1]
    discount_value = float(promotion[2] or 0)

    if discount_value <= 0:
        return jsonify({
            "success": False,
            "message": "Mã khuyến mãi chưa được cấu hình giá trị giảm."
        })

    discount_amount = 0

    if discount_type == "percent":
        discount_amount = original_price * discount_value / 100
    elif discount_type == "amount":
        discount_amount = discount_value

    if discount_amount > original_price:
        discount_amount = original_price

    total_price = original_price - discount_amount

    return jsonify({
        "success": True,
        "message": "Áp dụng mã khuyến mãi thành công!",
        "promotion_code": promotion_code,
        "original_price": original_price,
        "discount_amount": discount_amount,
        "total_price": total_price
    })
# =========================
# KHÁCH HÀNG - CHI TIẾT ĐƠN CỦA TÔI
# =========================
@app.route("/my-bookings/<int:booking_id>")
def my_booking_detail(booking_id):
    if "user_id" not in session:
        flash("Vui lòng đăng nhập để xem đơn của bạn!")
        return redirect(url_for("login"))

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT
            bookings.id,
            bookings.full_name,
            bookings.email,
            bookings.phone,
            bookings.number_of_people,
            bookings.note,
            CAST(bookings.original_price AS DECIMAL(12,2)),
            CAST(bookings.discount_amount AS DECIMAL(12,2)),
            bookings.promotion_code,
            CAST(bookings.total_price AS DECIMAL(12,2)),
            bookings.status,
            bookings.created_at,
            tours.name,
            tours.destination,
            tours.duration,
            tours.departure_date,
            tours.image,
            tours.description,
            tours.schedule
        FROM bookings
        JOIN tours ON bookings.tour_id = tours.id
        WHERE bookings.id = %s AND bookings.user_id = %s
    """, (booking_id, session["user_id"]))

    booking = cur.fetchone()
    cur.close()

    if not booking:
        flash("Không tìm thấy đơn đặt tour!")
        return redirect(url_for("my_bookings"))

    return render_template("customer/my_booking_detail.html", booking=booking)
# =========================
# CHATBOT GEMINI - TƯ VẤN TOUR TỪ MYSQL
# =========================
def extract_ai_json(raw_text):
    if not raw_text:
        return {"reply": "AI chưa có phản hồi.", "tour_ids": []}

    text = raw_text.strip()

    text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass

    return {
        "reply": text,
        "tour_ids": []
    }

@app.route("/chatbot", methods=["POST"])
def chatbot():
    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return {
            "success": False,
            "reply": "Bạn vui lòng nhập nội dung cần tư vấn.",
            "tours": []
        }

    if not GEMINI_API_KEY or gemini_client is None:
        return {
            "success": False,
            "reply": "Chưa cấu hình GEMINI_API_KEY trong file .env.",
            "tours": []
        }

    try:
        cur = mysql.connection.cursor()

        cur.execute("""
            SELECT 
                id,
                name,
                destination,
                price,
                duration,
                departure_date,
                description
            FROM tours
            WHERE status = 'active'
            ORDER BY price ASC
            LIMIT 8
        """)

        tours = cur.fetchall()

        tour_context = ""
        valid_tour_ids = []

        for tour in tours:
            valid_tour_ids.append(str(tour[0]))

            tour_context += f"""
ID: {tour[0]}
Tên tour: {tour[1]}
Điểm đến: {tour[2]}
Giá: {tour[3]}đ
Thời gian: {tour[4]}
Ngày khởi hành: {tour[5]}
Mô tả: {tour[6]}
---
"""

        prompt = f"""
Bạn là TravelAI Assistant, trợ lý tư vấn tour du lịch cho website TravelAI Booking.

Nhiệm vụ:
- Trả lời bằng tiếng Việt, ngắn gọn, thân thiện.
- Chỉ tư vấn dựa trên danh sách tour bên dưới.
- Không bịa tour ngoài dữ liệu.
- Nếu khách hỏi chung chung, hãy hỏi thêm ngân sách, điểm đến, số ngày hoặc sở thích.
- Nếu có tour phù hợp, hãy gợi ý tối đa 3 tour phù hợp nhất.
- Trả về JSON hợp lệ, không markdown.

Định dạng JSON:
{{
  "reply": "Nội dung tư vấn cho khách",
  "tour_ids": [1, 2, 3]
}}

Dữ liệu tour:
{tour_context}

Câu hỏi khách:
{user_message}
"""

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )

        raw_text = response.text or ""
        ai_data = extract_ai_json(raw_text)

        reply = ai_data.get("reply", "Tôi chưa có gợi ý phù hợp.")
        selected_ids = ai_data.get("tour_ids", [])

        selected_ids = [
            str(tour_id)
            for tour_id in selected_ids
            if str(tour_id) in valid_tour_ids
        ]

        suggested_tours = []

        if selected_ids:
            placeholders = ",".join(["%s"] * len(selected_ids))

            cur.execute(f"""
                SELECT 
                    id,
                    name,
                    destination,
                    price,
                    duration,
                    departure_date,
                    image
                FROM tours
                WHERE id IN ({placeholders})
            """, tuple(selected_ids))

            tour_rows = cur.fetchall()
            tour_dict = {str(t[0]): t for t in tour_rows}

            for tour_id in selected_ids:
                if tour_id in tour_dict:
                    t = tour_dict[tour_id]

                    suggested_tours.append({
                        "id": t[0],
                        "name": t[1],
                        "destination": t[2],
                        "price": float(t[3] or 0),
                        "duration": t[4],
                        "departure_date": str(t[5]),
                        "image": t[6],
                        "detail_url": f"/tour/{t[0]}"
                    })

        cur.close()

        return {
            "success": True,
            "reply": reply,
            "tours": suggested_tours
        }

    except Exception as e:
        print("Lỗi Gemini chatbot:", e)

        # Nếu Gemini lỗi hoặc quá tải, dùng gợi ý tour từ MySQL để web vẫn hoạt động
        try:
            fallback_tours = []

            cur = mysql.connection.cursor()
            cur.execute("""
                SELECT 
                    id,
                    name,
                    destination,
                    price,
                    duration,
                    departure_date,
                    image
                FROM tours
                WHERE status = 'active'
                ORDER BY price ASC
                LIMIT 3
            """)
            rows = cur.fetchall()
            cur.close()

            for t in rows:
                fallback_tours.append({
                    "id": t[0],
                    "name": t[1],
                    "destination": t[2],
                    "price": float(t[3] or 0),
                    "duration": t[4],
                    "departure_date": str(t[5]),
                    "image": t[6],
                    "detail_url": f"/tour/{t[0]}"
                })

            return {
                "success": True,
                "reply": "Hiện AI Gemini đang bận, nhưng TravelAI đã tìm nhanh một số tour giá tốt trong hệ thống để bạn tham khảo:",
                "tours": fallback_tours
            }

        except Exception as fallback_error:
            print("Lỗi fallback chatbot:", fallback_error)

            return {
                "success": False,
                "reply": "Trợ lý AI đang bận. Bạn vui lòng thử lại sau ít phút hoặc vào mục Tour trọn gói để xem danh sách tour.",
                "tours": []
            }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

