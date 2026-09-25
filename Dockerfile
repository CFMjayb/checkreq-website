FROM python:3.11-slim
# Closes CVE-2026-24049 (wheel) / CVE-2026-23949 (jaraco.context). The scanner
# flags the copies vendored INSIDE the base image's setuptools
# (setuptools/_vendor/wheel-0.45.1, jaraco.context-5.3.0), so upgrading the
# top-level packages alone does not clear them (tried 2026-09-22). setuptools
# 81.x is the first release vendoring fixed copies (wheel 0.46.3,
# jaraco_context 6.1.0); <82 because 82 removed pkg_resources (2026-09-25).
RUN pip install --no-cache-dir --upgrade "setuptools>=81,<82" "wheel>=0.46.2" "jaraco.context>=6.1.0"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
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
