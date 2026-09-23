FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Closes CVE-2026-24049 (wheel) / CVE-2026-23949 (jaraco-context) -- both are
# base-image pip toolchain packages, not in requirements.txt, so a plain
# rebuild never picks up the fix on its own (26-134 CVE plan, 2026-09-22).
RUN pip install --no-cache-dir --upgrade "wheel>=0.46.2" "jaraco-context>=6.1"
# Check-request PDF generation (render_check_voucher_pdf in main.py) uses a
# real headless Chromium via Playwright instead of xhtml2pdf (2026-07-25 --
# xhtml2pdf could not faithfully reproduce check_voucher.css's layout, see
# main.py's _html_to_pdf_bytes docstring). --with-deps installs the Debian
# system libraries Chromium needs (fonts, GTK/Cairo/etc.) via apt, which is
# available in this slim base image; needs no other setup on Cloud Run.
ENV DEBIAN_FRONTEND=noninteractive
RUN playwright install --with-deps chromium
COPY . .
ENV PORT=8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
