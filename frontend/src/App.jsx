import { useEffect, useMemo, useState } from "react";
import "./App.css";

const API_URL = "http://localhost:8000";

function resolveApiUrl(value) {
  if (!value || typeof value !== "string") return null;
  if (/^https?:\/\//i.test(value)) return value;
  return `${API_URL}${value.startsWith("/") ? "" : "/"}${value}`;
}

function getAnnotatedPdfPath(result) {
  return (
    result?.annotated_pdf_url ||
    result?.download_url ||
    result?.pdf_url ||
    result?.annotatedPdfUrl ||
    null
  );
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function App() {
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [zoom, setZoom] = useState(100);
  const [showNumbers, setShowNumbers] = useState(true);
  const [annotatedBlobUrl, setAnnotatedBlobUrl] = useState(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfPreviewError, setPdfPreviewError] = useState("");

  const previewUrl = useMemo(
    () => (file ? URL.createObjectURL(file) : null),
    [file]
  );

  const annotatedPdfPath = getAnnotatedPdfPath(result);
  const resolvedAnnotatedPdfUrl = resolveApiUrl(annotatedPdfPath);
  const isPdfFile = Boolean(
    file &&
      (file.type === "application/pdf" ||
        file.name.toLowerCase().endsWith(".pdf"))
  );
  const pages = Array.isArray(result?.pages) ? result.pages : [];
  const hasCoordinates = pages.some(
    (page) => Array.isArray(page.notes) && page.notes.length > 0
  );
  const hasImageOverlay = Boolean(!isPdfFile && hasCoordinates);
  const hasAnnotatedPdf = Boolean(resolvedAnnotatedPdfUrl);

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  // Fetch the annotated PDF as a Blob so it can be previewed in the iframe
  // even if the backend responds with Content-Disposition: attachment.
  useEffect(() => {
    let cancelled = false;
    let createdObjectUrl = null;

    setAnnotatedBlobUrl(null);
    setPdfPreviewError("");

    if (!resolvedAnnotatedPdfUrl) {
      setPdfLoading(false);
      return undefined;
    }

    setPdfLoading(true);

    async function loadAnnotatedPdf() {
      try {
        const response = await fetch(resolvedAnnotatedPdfUrl);
        const contentType = (response.headers.get("content-type") || "").toLowerCase();

        if (!response.ok) {
          let detail = `HTTP ${response.status} ${response.statusText}`.trim();
          try {
            const body = await response.json();
            detail = body.detail || body.message || detail;
          } catch {
            // Keep the HTTP status when the error response isn't JSON.
          }
          throw new Error(detail);
        }

        if (!contentType.includes("application/pdf")) {
          let detail = `Backend mengembalikan content-type "${contentType || "tidak diketahui"}", bukan PDF.`;
          try {
            const body = await response.clone().json();
            detail = body.detail || body.message || detail;
          } catch {
            // The response may be another non-JSON file type.
          }
          throw new Error(detail);
        }

        const blob = await response.blob();
        createdObjectUrl = URL.createObjectURL(blob);

        if (!cancelled) {
          setAnnotatedBlobUrl(createdObjectUrl);
          createdObjectUrl = null;
        }
      } catch (err) {
        if (!cancelled) {
          setPdfPreviewError(
            err?.message || "PDF anotasi gagal dimuat dari backend."
          );
        }
      } finally {
        if (!cancelled) setPdfLoading(false);
        if (createdObjectUrl) URL.revokeObjectURL(createdObjectUrl);
      }
    }

    loadAnnotatedPdf();

    return () => {
      cancelled = true;
      if (createdObjectUrl) URL.revokeObjectURL(createdObjectUrl);
    };
  }, [resolvedAnnotatedPdfUrl]);

  useEffect(() => {
    return () => {
      if (annotatedBlobUrl) URL.revokeObjectURL(annotatedBlobUrl);
    };
  }, [annotatedBlobUrl]);

  async function convertFile(event) {
    event.preventDefault();

    if (!file) {
      setError("Pilih file partitur terlebih dahulu.");
      return;
    }

    setLoading(true);
    setError("");
    setResult(null);
    setAnnotatedBlobUrl(null);
    setPdfPreviewError("");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch(`${API_URL}/api/convert`, {
        method: "POST",
        body: formData,
      });

      let data;
      try {
        data = await response.json();
      } catch {
        throw new Error(`Backend mengembalikan respons yang bukan JSON (HTTP ${response.status}).`);
      }

      if (!response.ok || data?.ok === false) {
        throw new Error(
          data.detail || data.message || `Konversi gagal (HTTP ${response.status}).`
        );
      }

      setResult(data);
    } catch (err) {
      setError(
        err?.message ||
          "Tidak dapat terhubung ke backend. Pastikan server berjalan di port 8000."
      );
    } finally {
      setLoading(false);
    }
  }

  function downloadText() {
    if (!result || !Array.isArray(result.parts)) return;

    const text = result.parts
      .map(
        (part) =>
          `${part.part}\n${(part.events || [])
            .map((event) => event.number)
            .join(" ")}`
      )
      .join("\n\n");

    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "not-angka.txt";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadAnnotatedPdf() {
    if (annotatedBlobUrl) {
      const link = document.createElement("a");
      link.href = annotatedBlobUrl;
      link.download = "partitur-not-angka.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      return;
    }

    if (resolvedAnnotatedPdfUrl) {
      window.open(resolvedAnnotatedPdfUrl, "_blank", "noopener,noreferrer");
    }
  }

  function chooseNewFile(event) {
    setFile(event.target.files?.[0] || null);
    setResult(null);
    setError("");
    setPdfPreviewError("");
    setAnnotatedBlobUrl(null);
    setZoom(100);
    setShowNumbers(true);
  }

  const shouldShowUploadedOriginal = !result && !error;
  const shouldShowAnnotatedPdf = Boolean(result && hasAnnotatedPdf && annotatedBlobUrl);
  const shouldShowOverlayImage = Boolean(result && hasImageOverlay);
  const noOutputPreview = Boolean(
    result && !shouldShowAnnotatedPdf && !shouldShowOverlayImage
  );

  return (
    <main className="app">
      <header className="header">
        <div className="logo">♪</div>
        <div>
          <h1>Sheet to Number</h1>
          <p>Turn piano sheet music into Indonesian number notation.</p>
        </div>
      </header>

      <section className="panel upload-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">MUSIC CONVERTER</p>
            <h2>Upload your sheet music</h2>
            <p className="muted">
              Upload a clear piano score in PDF, JPG, or PNG format.
            </p>
          </div>
          <span className="music-icon">♫</span>
        </div>

        <form onSubmit={convertFile}>
          <label className="upload">
            <span className="upload-icon">↑</span>
            <strong>{file ? file.name : "Choose a sheet music file"}</strong>
            <span className="muted">
              PDF, JPG, PNG · Maximum 15 MB
              {file?.size ? ` · ${formatBytes(file.size)}` : ""}
            </span>
            <input
              type="file"
              accept=".pdf,.png,.jpg,.jpeg"
              onChange={chooseNewFile}
            />
          </label>

          <button type="submit" disabled={loading || !file}>
            {loading ? "Reading sheet music..." : "Convert sheet music"}
          </button>
        </form>

        {loading && (
          <div className="loading-message" role="status" aria-live="polite">
            <span className="spinner" />
            <span>
              Reading notes and musical structure. This may take a moment.
            </span>
          </div>
        )}

        {error && (
          <div className="error" role="alert">
            <strong>Konversi gagal</strong>
            <p>{error}</p>
          </div>
        )}
      </section>

      {file && (
        <section className="panel score-panel">
          <div className="score-header">
            <div>
              <p className="eyebrow">
                {result ? "CONVERSION RESULT" : "SCORE PREVIEW"}
              </p>
              <h2>{file.name}</h2>
              <p className="muted">
                {result
                  ? `Key: ${result.key ?? "—"} · Tonic: ${result.tonic ?? "—"}`
                  : "Preview file yang diunggah sebelum konversi."}
              </p>
            </div>

            {result && (
              <span className="status-pill">
                <span className="status-dot" />
                Backend response received
              </span>
            )}
          </div>

          <div className="toolbar">
            <div className="zoom-controls">
              <button
                className="icon-button"
                type="button"
                aria-label="Zoom out"
                onClick={() => setZoom((value) => Math.max(50, value - 10))}
              >
                −
              </button>
              <span>{zoom}%</span>
              <button
                className="icon-button"
                type="button"
                aria-label="Zoom in"
                onClick={() => setZoom((value) => Math.min(200, value + 10))}
              >
                +
              </button>
              <button
                className="text-button"
                type="button"
                onClick={() => setZoom(100)}
              >
                Reset
              </button>
            </div>

            {shouldShowOverlayImage && (
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={showNumbers}
                  onChange={(event) => setShowNumbers(event.target.checked)}
                />
                Show numbers
              </label>
            )}
          </div>

          <div className="score-viewport">
            {shouldShowAnnotatedPdf ? (
              <div className="score-page" style={{ width: `${zoom}%` }}>
                <iframe
                  title="Annotated sheet music PDF"
                  src={annotatedBlobUrl}
                  className="pdf-frame"
                />
              </div>
            ) : shouldShowOverlayImage ? (
              <div className="score-page" style={{ width: `${zoom}%` }}>
                <div className="image-score">
                  <img src={previewUrl} alt="Uploaded sheet music with number notation overlay" />
                  {showNumbers &&
                    pages.map((page, pageIndex) =>
                      (page.notes || []).map((item, index) => (
                        <span
                          className="coordinate-number"
                          key={`${pageIndex}-${index}`}
                          style={{
                            left: `${(item.x / page.pageWidth) * 100}%`,
                            top: `${(item.y / page.pageHeight) * 100}%`,
                          }}
                        >
                          {item.number}
                        </span>
                      ))
                    )}
                </div>
              </div>
            ) : shouldShowUploadedOriginal ? (
              <div className="score-page" style={{ width: `${zoom}%` }}>
                {isPdfFile ? (
                  <iframe
                    title="Uploaded original sheet music preview"
                    src={previewUrl}
                    className="pdf-frame"
                  />
                ) : (
                  <div className="image-score">
                    <img src={previewUrl} alt="Uploaded original sheet music preview" />
                  </div>
                )}
              </div>
            ) : error ? (
              <div className="notice" role="alert">
                <strong>Preview hasil tidak tersedia karena konversi gagal</strong>
                <p>{error}</p>
                <p>File asli disembunyikan di bagian hasil agar tidak terlihat seolah-olah sudah berhasil dianotasi.</p>
              </div>
            ) : (
              <div className="notice" role="status">
                <strong>
                  {pdfLoading
                    ? "Memuat PDF hasil anotasi..."
                    : "Hasil belum bisa dipratinjau"}
                </strong>
                <p>
                  File asli sengaja tidak ditampilkan sebagai hasil konversi. Bagian ini hanya akan menampilkan PDF yang sudah dianotasi atau gambar dengan koordinat not.
                </p>
                {pdfPreviewError && (
                  <p>
                    <strong>Error preview PDF:</strong> {pdfPreviewError}
                  </p>
                )}
                {result?.coordinate_message && <p>{result.coordinate_message}</p>}
                {result?.message && <p>{result.message}</p>}
                {result?.detail && <p>{String(result.detail)}</p>}
                {result?.pdf_error && <p>PDF error: {String(result.pdf_error)}</p>}
                {result?.annotated_pdf_url || result?.download_url ? (
                  <p>
                    Backend memberikan URL PDF, tetapi file gagal dimuat untuk preview.
                  </p>
                ) : (
                  <p>
                    Backend belum mengirim <code>annotated_pdf_url</code> atau <code>download_url</code>.
                  </p>
                )}
                <details style={{ marginTop: 12 }}>
                  <summary>Detail respons backend</summary>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      overflowWrap: "anywhere",
                      maxHeight: 320,
                      overflow: "auto",
                      padding: 12,
                      borderRadius: 8,
                      background: "rgba(127, 127, 127, 0.10)",
                      fontSize: 12,
                    }}
                  >
                    {JSON.stringify(
                      {
                        ok: result?.ok,
                        key: result?.key,
                        tonic: result?.tonic,
                        totalEvents: result?.totalEvents,
                        partCount: result?.parts?.length,
                        coordinates_available: result?.coordinates_available,
                        coordinate_message: result?.coordinate_message,
                        annotated_pdf_url: result?.annotated_pdf_url,
                        download_url: result?.download_url,
                        pdf_url: result?.pdf_url,
                        message: result?.message,
                        detail: result?.detail,
                        pdf_error: result?.pdf_error,
                      },
                      null,
                      2
                    )}
                  </pre>
                </details>
              </div>
            )}
          </div>

          <div className="download-actions">
            <button
              type="button"
              className="secondary"
              onClick={downloadText}
              disabled={!result || !Array.isArray(result.parts)}
            >
              Download notation (.txt)
            </button>

            <button
              type="button"
              onClick={downloadAnnotatedPdf}
              disabled={!hasAnnotatedPdf}
              title={!hasAnnotatedPdf ? "Backend belum mengirim URL PDF anotasi" : "Download annotated PDF"}
            >
              {pdfLoading ? "Preparing PDF..." : "Download annotated PDF"}
            </button>
          </div>
        </section>
      )}

      {result && (
        <section className="panel summary-panel">
          <p className="eyebrow">READING SUMMARY</p>
          <h2>Conversion details</h2>

          <div className="summary-grid">
            <div className="summary-card">
              <span className="muted">Key signature</span>
              <strong>{result.key ?? "—"}</strong>
            </div>
            <div className="summary-card">
              <span className="muted">Tonic</span>
              <strong>{result.tonic ?? "—"}</strong>
            </div>
            <div className="summary-card">
              <span className="muted">Musical events</span>
              <strong>{result.totalEvents ?? "—"}</strong>
            </div>
            <div className="summary-card">
              <span className="muted">Parts detected</span>
              <strong>{Array.isArray(result.parts) ? result.parts.length : "—"}</strong>
            </div>
          </div>

          <p className="muted summary-note">
            {(result.parts || []).map((part, index) => (
              <span key={`${part.part}-${index}`}>
                {index > 0 ? " · " : ""}
                {part.part}: {part.eventCount ?? part.events?.length ?? 0} events
              </span>
            ))}
          </p>

          {result.coordinates_available === false && (
            <div className="notice">
              <strong>Koordinat not belum tersedia dari backend</strong>
              <p>{result.coordinate_message || "Backend belum mengirim koordinat notehead."}</p>
            </div>
          )}
        </section>
      )}

      <footer>
        Sheet to Number <span>·</span> Music notation workspace
      </footer>
    </main>
  );
}
