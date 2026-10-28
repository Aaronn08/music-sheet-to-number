import os
import shutil
import subprocess
import tempfile
import traceback
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import defusedxml
defusedxml.defuse_stdlib()

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from music21 import chord, converter, key, note, stream

app = FastAPI(title="Sheet to Number API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_FILE_SIZE = 15 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Folder permanen untuk memeriksa hasil internal Audiveris.
DEBUG_DIR = BASE_DIR / "debug_audiveris"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# AUDIVERIS
# ---------------------------------------------------------

def get_audiveris_command():
    command = os.getenv("AUDIVERIS_CMD", "audiveris")

    if Path(command).is_file():
        return command

    return shutil.which(command)


# ---------------------------------------------------------
# KEY DETECTION
# ---------------------------------------------------------

def detect_key(score):
    """
    Gunakan key signature yang ditemukan.
    Jika tidak ada, coba analisis tonalitas dari musik.
    """

    signatures = list(
        score.recurse().getElementsByClass(key.KeySignature)
    )

    if signatures:
        try:
            detected = signatures[0].asKey("major")

            if detected is not None and detected.tonic is not None:
                return detected

        except Exception:
            print("[KEY] Key signature gagal dibaca.")
            traceback.print_exc()

    try:
        detected = score.analyze("key")

        if detected is not None and detected.tonic is not None:
            return detected

    except Exception:
        print("[KEY] Analisis tonalitas gagal.")
        traceback.print_exc()

    raise ValueError(
        "Nada dasar tidak dapat ditentukan dari partitur."
    )


# ---------------------------------------------------------
# NUMBER NOTATION
# ---------------------------------------------------------

def degree_for_pitch(pitch, tonic=None):
    """Map note letters to fixed Indonesian numeric notation (C-major reference).

    This mode intentionally ignores the detected tonic, key signature, sharps,
    flats, and local accidentals for the numeric degree. The mapping is fixed:
    C=1, D=2, E=3, F=4, G=5, A=6, B=7.

    Octave marks are based on middle C's octave (C4-B4 have no dot;
    octave 5 and above get dots above; octave 3 and below get dots below).
    """
    letter_to_number = {
        "C": "1",
        "D": "2",
        "E": "3",
        "F": "4",
        "G": "5",
        "A": "6",
        "B": "7",
    }

    step = getattr(pitch, "step", None)
    if step not in letter_to_number:
        # music21 Pitch usually always provides .step; this fallback keeps
        # the function resilient to a pitch-like object.
        name = getattr(pitch, "name", "")
        step = name[:1] if name else None

    if step not in letter_to_number:
        raise ValueError(f"Huruf nada tidak dikenali: {step!r}")

    number = letter_to_number[step]
    octave = getattr(pitch, "octave", None)
    if octave is not None:
        if octave > 4:
            number += "\u0307" * (octave - 4)
        elif octave < 4:
            number = "\u0323" * (4 - octave) + number

    return number


# ---------------------------------------------------------
# MUSICXML HELPERS
# ---------------------------------------------------------

def get_measure_number(element):
    """
    Ambil nomor birama jika informasi tersedia.
    """

    try:
        measure = element.getContextByClass(stream.Measure)

        if measure is not None and measure.number is not None:
            return measure.number

    except Exception:
        pass

    return None


def get_staff_number(element):
    """
    Ambil nomor staff jika tersedia pada objek music21.
    Nilai dapat None jika MusicXML tidak menyediakannya.
    """

    try:
        staff = getattr(element, "staffNumber", None)

        if staff is not None:
            return staff
    except Exception:
        pass

    return None


def convert_element(element, tonic):
    """
    Konversi satu not, chord, atau rest.
    Simpan posisi waktunya agar frontend dapat
    menyelaraskan event musik.
    """

    duration = float(element.duration.quarterLength)
    offset = float(element.getOffsetInHierarchy(element.activeSite))

    # Fallback offset jika struktur stream tidak biasa.
    if offset < 0:
        offset = float(element.offset)

    measure_number = get_measure_number(element)
    staff_number = get_staff_number(element)

    common = {
        "offset": offset,
        "quarterLength": duration,
        "measure": measure_number,
        "staff": staff_number,
    }

    if isinstance(element, note.Rest):
        return {
            **common,
            "type": "rest",
            "number": "0",
            "pitches": [],
        }

    if isinstance(element, chord.Chord):
        pitches = sorted(
            element.pitches,
            key=lambda p: (p.octave if p.octave is not None else 0,
                           p.pitchClass),
        )

        numbers = [
            degree_for_pitch(p, tonic)
            for p in pitches
        ]

        return {
            **common,
            "type": "chord",
            "number": "(" + " ".join(numbers) + ")",
            "pitches": [
                {
                    "name": p.nameWithOctave,
                    "midi": p.midi,
                    "number": degree_for_pitch(p, tonic),
                }
                for p in pitches
            ],
        }

    if isinstance(element, note.Note):
        return {
            **common,
            "type": "note",
            "number": degree_for_pitch(element.pitch, tonic),
            "pitches": [
                {
                    "name": element.pitch.nameWithOctave,
                    "midi": element.pitch.midi,
                    "number": degree_for_pitch(element.pitch, tonic),
                }
            ],
        }

    return None


# ---------------------------------------------------------
# SCORE CONVERSION
# ---------------------------------------------------------

def convert_score(score):
    """
    Ubah score MusicXML menjadi struktur JSON.
    Setiap part diproses terpisah agar tangan kanan
    dan tangan kiri tidak digabungkan.
    """

    detected_key = detect_key(score)
    tonic = detected_key.tonic

    parts = []

    for index, part in enumerate(score.parts):
        events = []

        # flatten() memudahkan pembacaan seluruh event
        # tanpa menghilangkan urutan waktu.
        flattened = part.flatten()

        elements = sorted(
            flattened.notesAndRests,
            key=lambda element: (
                float(element.offset),
                0 if isinstance(element, note.Note) else 1,
            ),
        )

        for element in elements:
            converted = convert_element(element, tonic)

            if converted is not None:
                events.append(converted)

        part_name = (
            part.partName
            or part.id
            or f"Part {index + 1}"
        )

        parts.append({
            "index": index,
            "part": str(part_name),
            "eventCount": len(events),
            "events": events,
        })

        print(
            f"[PART {index + 1}] "
            f"Nama={part_name!r}, "
            f"Jumlah event={len(events)}"
        )

    if not parts:
        raise ValueError(
            "MusicXML berhasil dibaca, tetapi tidak ada part musik."
        )

    total_events = sum(
        len(part["events"])
        for part in parts
    )

    if total_events == 0:
        raise ValueError(
            "Tidak ditemukan not atau tanda istirahat."
        )

    return {
        "key": str(detected_key),
        "tonic": tonic.name,
        "parts": parts,
        "totalEvents": total_events,
    }


# ---------------------------------------------------------
# AUDIVERIS NOTEHEAD COORDINATES + PDF ANNOTATION
# ---------------------------------------------------------

def local_tag(tag):
    """Nama tag XML tanpa namespace, kalau ada."""
    return tag.rsplit("}", 1)[-1]


def _ancestor_with_tag(element, parents, wanted_tag):
    """Return the nearest ancestor with the requested local XML tag."""
    node = element
    while node is not None:
        if local_tag(node.tag) == wanted_tag:
            return node
        node = parents.get(node)
    return None


def _key_signature_alterations(fifths):
    """Map key-signature fifths to letter accidentals."""
    sharp_order = ("F", "C", "G", "D", "A", "E", "B")
    flat_order = ("B", "E", "A", "D", "G", "C", "F")
    alterations = {}
    try:
        count = int(fifths or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        alterations.update({letter: 1 for letter in sharp_order[:min(count, 7)]})
    elif count < 0:
        alterations.update({letter: -1 for letter in flat_order[:min(abs(count), 7)]})
    return alterations


def _local_accidentals_by_head(root, by_id):
    """Read explicit accidental-to-notehead links from Audiveris relations."""
    result = {}
    shape_to_alter = {
        "SHARP": 1,
        "FLAT": -1,
        "NATURAL": 0,
        "DOUBLE_SHARP": 2,
        "DOUBLE_FLAT": -2,
    }
    for relation in root.iter():
        if local_tag(relation.tag) != "relation":
            continue
        relation_type = next(
            (child for child in relation if local_tag(child.tag) == "alter-head"),
            None,
        )
        if relation_type is None:
            continue
        source = relation.attrib.get("source", "")
        target = relation.attrib.get("target", "")
        source_el = by_id.get(source)
        target_el = by_id.get(target)
        source_is_head = source_el is not None and local_tag(source_el.tag) == "head"
        target_is_head = target_el is not None and local_tag(target_el.tag) == "head"
        if source_is_head == target_is_head:
            continue
        head_id = source if source_is_head else target
        accidental_el = target_el if source_is_head else source_el
        shape = accidental_el.attrib.get("shape", "") if accidental_el is not None else ""
        if shape in shape_to_alter:
            result[head_id] = shape_to_alter[shape]
    return result


def _head_measure_map(root, parents, page_num):
    """Map notehead IDs to the OMR measure stack that contains their chord."""
    result = {}
    for measure in root.iter():
        if local_tag(measure.tag) != "measure":
            continue
        system = _ancestor_with_tag(measure, parents, "system")
        part = _ancestor_with_tag(measure, parents, "part")
        system_id = system.attrib.get("id", "?") if system is not None else "?"
        part_id = part.attrib.get("id", "?") if part is not None else "?"
        measure_id = measure.attrib.get("id", "?")
        measure_key = f"{page_num}:{system_id}:{part_id}:{measure_id}"
        for child in measure.iter():
            if local_tag(child.tag) not in {"head-chord", "small-chord"}:
                continue
            notes_el = next((n for n in child if local_tag(n.tag) == "notes"), None)
            if notes_el is None or not (notes_el.text or "").strip():
                continue
            for head_id in (notes_el.text or "").split():
                result[head_id] = measure_key
    return result


def extract_noteheads(omr_path, fallback_key_fifths=0):
    """Extract notehead geometry and staff pitch metadata.

    Some Audiveris sheet XML exports omit the key signature from a staff header.
    When that happens, use the key signature inferred from MusicXML so key-signature
    pitches (for example F-sharp in D major) are not labelled as flat notes.
    """
    found = []
    with zipfile.ZipFile(omr_path) as archive:
        sheet_xmls = [
            name for name in archive.namelist()
            if name.startswith("sheet#")
            and name.lower().endswith(".xml")
            and "/sheet#" in name
            and name.rsplit("/", 1)[-1].startswith("sheet#")
        ]
        sheet_xmls.sort(key=lambda n: int(n.split("sheet#", 1)[1].split("/", 1)[0]))

        for sheet_xml in sheet_xmls:
            try:
                page_num = int(sheet_xml.split("sheet#", 1)[1].split("/", 1)[0])
                root = ET.fromstring(archive.read(sheet_xml))
            except (ValueError, ET.ParseError, KeyError):
                continue

            elements = list(root.iter())
            parents = {child: parent for parent in elements for child in parent}
            by_id = {el.attrib["id"]: el for el in elements if el.attrib.get("id")}
            local_accidentals = _local_accidentals_by_head(root, by_id)
            measure_map = _head_measure_map(root, parents, page_num)

            # Audiveris staff IDs are ranked across the whole sheet, not reset to
            # 1=treble / 2=bass for every system. Build a lookup from each physical
            # staff ID to the clef and key signature stored in that staff's header.
            # This is the important fix for lower staves in later systems.
            staff_metadata = {}
            for staff_node in elements:
                if local_tag(staff_node.tag) != "staff":
                    continue
                staff_id = str(staff_node.attrib.get("id", "")).strip()
                if not staff_id:
                    continue
                staff_header = next(
                    (child for child in staff_node if local_tag(child.tag) == "header"),
                    None,
                )
                if staff_header is None:
                    continue
                staff_clef = None
                staff_key_fifths = None
                for header_child in staff_header:
                    tag = local_tag(header_child.tag)
                    if tag == "clef":
                        candidate = header_child.attrib.get("kind", "").upper()
                        if candidate in {"TREBLE", "BASS", "ALTO", "TENOR"}:
                            staff_clef = candidate
                    elif tag == "key" and "fifths" in header_child.attrib:
                        try:
                            staff_key_fifths = int(header_child.attrib["fifths"])
                        except (TypeError, ValueError):
                            pass
                staff_metadata[staff_id] = {
                    "clef_kind": staff_clef,
                    "key_fifths": staff_key_fifths,
                }

            for el in elements:
                if local_tag(el.tag) != "head":
                    continue
                glyph = by_id.get(el.attrib.get("glyph", ""))
                if glyph is None:
                    continue
                run_table = next(
                    (child for child in glyph if local_tag(child.tag) == "run-table"),
                    None,
                )
                if run_table is None:
                    continue
                try:
                    left = float(glyph.attrib["left"])
                    top = float(glyph.attrib["top"])
                    width = float(run_table.attrib["width"])
                    height = float(run_table.attrib["height"])
                    staff_num = int(el.attrib.get("staff", "1"))
                    staff_pitch = float(el.attrib["pitch"])
                except (KeyError, ValueError):
                    continue

                staff_el = _ancestor_with_tag(el, parents, "staff")
                system_el = _ancestor_with_tag(el, parents, "system")
                part_el = _ancestor_with_tag(el, parents, "part")
                header = None
                if staff_el is not None:
                    header = next(
                        (child for child in staff_el if local_tag(child.tag) == "header"),
                        None,
                    )
                # The head may not be nested beneath its <staff> container in every
                # Audiveris XML version. Therefore resolve the physical staff using
                # head @staff -> staff_metadata first; only then use ancestor headers.
                fallback_fifths = int(fallback_key_fifths or 0)
                staff_info = staff_metadata.get(str(staff_num), {})
                clef_kind = staff_info.get("clef_kind")
                staff_header_fifths = staff_info.get("key_fifths")

                # If this head is nested in a staff, prefer its attached header when it
                # has a real clef; the global staff-ID lookup handles detached heads.
                fifths = fallback_fifths
                if header is not None:
                    clef_el = next(
                        (child for child in header if local_tag(child.tag) == "clef"),
                        None,
                    )
                    if clef_el is not None:
                        candidate_clef = clef_el.attrib.get("kind", "").upper()
                        if candidate_clef in {"TREBLE", "BASS", "ALTO", "TENOR"}:
                            clef_kind = candidate_clef
                    key_el = next(
                        (child for child in header if local_tag(child.tag) == "key"),
                        None,
                    )
                    if key_el is not None and "fifths" in key_el.attrib:
                        try:
                            audiveris_fifths = int(key_el.attrib["fifths"])
                            if audiveris_fifths != 0 or fallback_fifths == 0:
                                fifths = audiveris_fifths
                        except (TypeError, ValueError):
                            pass
                    elif staff_header_fifths is not None:
                        audiveris_fifths = staff_header_fifths
                        if audiveris_fifths != 0 or fallback_fifths == 0:
                            fifths = audiveris_fifths
                elif staff_header_fifths is not None:
                    audiveris_fifths = staff_header_fifths
                    if audiveris_fifths != 0 or fallback_fifths == 0:
                        fifths = audiveris_fifths

                # Last-resort fallback for piano grand-staff scores: staff IDs are
                # ranked top-to-bottom across the sheet, so even IDs are typically
                # bass/lower staves and odd IDs treble/upper staves. Explicit clefs
                # from staff_metadata always take priority over this heuristic.
                if clef_kind is None:
                    clef_kind = "BASS" if staff_num % 2 == 0 else "TREBLE"

                found.append({
                    "page": page_num,
                    "system": system_el.attrib.get("id", "?") if system_el is not None else "?",
                    "part_id": part_el.attrib.get("id", "?") if part_el is not None else "?",
                    "measure_key": measure_map.get(
                        el.attrib.get("id", ""),
                        f"{page_num}:{system_el.attrib.get('id', '?') if system_el is not None else '?'}:{part_el.attrib.get('id', '?') if part_el is not None else '?'}:system",
                    ),
                    "head_id": el.attrib.get("id", ""),
                    "x": left + width / 2.0,
                    "y": top + height / 2.0,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "staff": staff_num,
                    "staff_pitch": staff_pitch,
                    "clef_kind": clef_kind,
                    "key_fifths": fifths,
                    "local_alter": local_accidentals.get(el.attrib.get("id", "")),
                    "shape": el.attrib.get("shape", ""),
                    "glyph": el.attrib.get("glyph", ""),
                })
    return found


def sort_heads_reading_order(heads, system_gap=100.0):
    """Sort noteheads by page, system, staff, and horizontal reading order."""
    return sorted(
        heads,
        key=lambda h: (
            h["page"], str(h.get("system", "?")), str(h.get("part_id", "?")),
            h["staff"], h["x"], h["y"],
        ),
    )


def _staff_position_to_pitch(head, accidental_state):
    """Convert Audiveris staff position (not a sequential guess) to music21 Pitch.

    In Audiveris OMR, pitch 0 is the middle staff line, -4 the top line,
    and +4 the bottom line. Piano clefs therefore use B4 (treble) and D3 (bass)
    as the middle-line references.
    """
    from music21 import pitch as m21pitch

    middle_line = {
        "TREBLE": ("B", 4),
        "BASS": ("D", 3),
        "ALTO": ("C", 4),
        "TENOR": ("A", 3),
    }
    if head["clef_kind"] not in middle_line:
        raise ValueError(f"Clef belum didukung: {head['clef_kind']}")

    base_step, base_octave = middle_line[head["clef_kind"]]
    letter_order = ("C", "D", "E", "F", "G", "A", "B")
    base_diatonic = base_octave * 7 + letter_order.index(base_step)
    target_diatonic = base_diatonic - int(round(head["staff_pitch"]))
    octave, letter_index = divmod(target_diatonic, 7)
    letter = letter_order[letter_index]

    key_alter = _key_signature_alterations(head.get("key_fifths", 0)).get(letter, 0)
    state_key = (head["measure_key"], head["staff"], letter, octave)
    if head.get("local_alter") is not None:
        alteration = head["local_alter"]
        accidental_state[state_key] = alteration
    elif state_key in accidental_state:
        alteration = accidental_state[state_key]
    else:
        alteration = key_alter

    result = m21pitch.Pitch()
    result.step = letter
    result.octave = octave
    result.accidental = m21pitch.Accidental(alteration) if alteration else None
    return result

def draw_number_label(page, fitz, label, center_x, note_top, note_height, fontsize):
    """Draw ASCII number/accidental and represent octave marks as small dots.

    PyMuPDF's built-in Helvetica is not reliable for Unicode combining dots, so
    dots are drawn as vector circles rather than sent to the font renderer.
    """
    above = label.count("\u0307")
    below = label.count("\u0323")
    base = label.replace("\u0307", "").replace("\u0323", "")
    if not base:
        return

    text_width = fitz.get_text_length(base, fontname="helv", fontsize=fontsize)
    x = max(1.0, min(page.rect.width - text_width - 1.0, center_x - text_width / 2.0))
    baseline = max(fontsize + 1.0, note_top - 1.5)
    page.insert_text(
        (x, baseline), base, fontsize=fontsize, fontname="helv",
        color=(0.88, 0.02, 0.16), overlay=True,
    )

    # Baseline is the bottom of the text. Place octave dots around its center.
    radius = max(0.45, fontsize * 0.075)
    digit_center_x = x + text_width / 2.0
    if above:
        first_y = baseline - fontsize * 0.90
        for i in range(above):
            page.draw_circle(
                fitz.Point(digit_center_x + (i - (above - 1) / 2.0) * fontsize * 0.20, first_y),
                radius, color=(0.88, 0.02, 0.16), fill=(0.88, 0.02, 0.16), overlay=True,
            )
    if below:
        first_y = baseline + fontsize * 0.08
        for i in range(below):
            page.draw_circle(
                fitz.Point(digit_center_x + (i - (below - 1) / 2.0) * fontsize * 0.20, first_y),
                radius, color=(0.88, 0.02, 0.16), fill=(0.88, 0.02, 0.16), overlay=True,
            )


def create_annotated_pdf(
    source_path, suffix, omr_path, score, tonic, output_path,
    fallback_key_fifths=0,
):
    """Overlay number labels onto the original PDF using Audiveris notehead geometry.

    Use the MusicXML-derived key signature as a fallback if Audiveris omits it
    from a staff header. Visually review the output because OMR can misread symbols.
    """
    try:
        import fitz  # PyMuPDF
        from PIL import Image
        import io
    except ImportError as exc:
        raise RuntimeError(
            "Dependensi PDF belum terpasang. Jalankan: pip install pymupdf pillow"
        ) from exc

    if suffix == ".pdf":
        original = fitz.open(str(source_path))
    else:
        with Image.open(source_path) as image_in:
            image = image_in.convert("RGB")
            buf = io.BytesIO()
            image.save(buf, format="PDF", resolution=300)
        original = fitz.open(stream=buf.getvalue(), filetype="pdf")

    try:
        with zipfile.ZipFile(omr_path) as archive:
            names = archive.namelist()
            dimensions = {}
            for page_index in range(len(original)):
                page_num = page_index + 1
                image_name = f"sheet#{page_num}/BINARY.png"
                if image_name not in names:
                    image_name = next(
                        (name for name in names if name.startswith(f"sheet#{page_num}/") and name.endswith("/BINARY.png")),
                        None,
                    )
                if image_name:
                    with Image.open(io.BytesIO(archive.read(image_name))) as image:
                        dimensions[page_num] = image.size

        heads = extract_noteheads(omr_path, fallback_key_fifths=fallback_key_fifths)
        if not heads:
            raise RuntimeError(
                "Koordinat kepala not tidak ditemukan pada XML OMR Audiveris."
            )
        heads = sort_heads_reading_order(heads)

        # Generate each number from that notehead's staff position, clef and
        # key signature. Do not consume a parallel note sequence: one missed OMR
        # head must not shift every label that follows it.
        accidental_state = {}
        drawn = 0
        skipped = 0
        bass_heads_labeled = 0
        for head in heads:
            page_num = head["page"]
            page_index = page_num - 1
            if page_index < 0 or page_index >= len(original):
                continue
            dimensions_for_page = dimensions.get(page_num)
            if not dimensions_for_page:
                skipped += 1
                continue

            try:
                detected_pitch = _staff_position_to_pitch(head, accidental_state)
                label = degree_for_pitch(detected_pitch, tonic)
                # Both hands use the same pitch-to-number mapping. The clef is used
                # to identify the actual written pitch; do NOT shift bass degrees.
                # This keeps a D note labelled 2 in either hand, regardless of octave.
                if str(head.get("clef_kind", "")).upper() == "BASS":
                    bass_heads_labeled += 1
            except Exception as exc:
                print(
                    f"[ANNOTATION] Skip head={head.get('head_id')} "
                    f"staff={head.get('staff')} reason={exc}"
                )
                skipped += 1
                continue

            page = original[page_index]
            image_width, image_height = dimensions_for_page
            scale_x = page.rect.width / image_width
            scale_y = page.rect.height / image_height
            center_x = head["x"] * scale_x
            note_top = head["top"] * scale_y
            note_height = head["height"] * scale_y
            fontsize = max(6.0, min(9.0, page.rect.width / 65.0))
            draw_number_label(page, fitz, label, center_x, note_top, note_height, fontsize)
            drawn += 1

        staff_label_counts = {}
        for head in heads:
            clef_label = str(head.get("clef_kind", "UNKNOWN")).upper()
            staff_label_counts[clef_label] = staff_label_counts.get(clef_label, 0) + 1
        print(f"[ANNOTATION STAFF MAP] noteheads_by_clef={staff_label_counts}")
        print(
            f"[ANNOTATION] Noteheads={len(heads)}, drawn={drawn}, skipped={skipped}, "
            f"bass_heads_labeled={bass_heads_labeled}; both hands use the same pitch-to-number mapping."
        )

        if drawn == 0:
            raise RuntimeError(
                "Tidak ada angka yang ditambahkan. Periksa apakah dimensi BINARY.png cocok dengan jumlah halaman PDF."
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        original.save(str(output_path), garbage=4, deflate=True)
        return drawn
    finally:
        original.close()


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@app.get("/api/health")
def health():
    audiveris = get_audiveris_command()
    return {
        "ok": True,
        "service": "sheet-to-number",
        "audiveris_available": audiveris is not None,
        "audiveris_command": audiveris,
    }


# ---------------------------------------------------------
# CONVERT ENDPOINT
# ---------------------------------------------------------

@app.post("/api/convert")
async def convert_file(file: UploadFile = File(...)):
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Gunakan file PDF, PNG, JPG, atau JPEG.",
        )

    data = await file.read(MAX_FILE_SIZE + 1)
    if not data:
        raise HTTPException(status_code=400, detail="File kosong.")
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="Ukuran file maksimum 15 MB.",
        )

    audiveris = get_audiveris_command()
    if not audiveris:
        raise HTTPException(
            status_code=503,
            detail=(
                "Audiveris tidak ditemukan. Atur AUDIVERIS_CMD "
                "ke lokasi executable Audiveris."
            ),
        )

    print(f"\\n[UPLOAD] {filename} ({len(data)} bytes)")
    print(f"[AUDIVERIS] Executable: {audiveris}")

    with tempfile.TemporaryDirectory(prefix="sheet-to-number-") as temp:
        root = Path(temp)
        source = root / f"input{suffix}"
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)

        command = [
            audiveris,
            "-batch",
            "-export",
            "-output",
            str(output),
            str(source),
        ]
        print("[AUDIVERIS] Memulai pembacaan partitur...")

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail="Audiveris terlalu lama memproses file (timeout 180 detik).",
            )
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Audiveris gagal dijalankan: {exc}",
            )

        print(f"[AUDIVERIS] Exit code: {process.returncode}")
        if process.stdout.strip():
            print("[AUDIVERIS STDOUT]\\n" + process.stdout[-5000:])
        if process.stderr.strip():
            print("[AUDIVERIS STDERR]\\n" + process.stderr[-5000:])

        # Audiveris normally writes MusicXML/MXL under output/.
        candidates = sorted(
            [
                path for path in output.rglob("*")
                if path.is_file()
                and path.suffix.lower() in {".mxl", ".musicxml", ".xml"}
            ],
            key=lambda path: (
                path.suffix.lower() != ".mxl",
                str(path),
            ),
        )

        # Some Audiveris versions may place exports beside the output folder.
        if not candidates:
            candidates = sorted(
                [
                    path for path in root.rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in {".mxl", ".musicxml", ".xml"}
                ],
                key=lambda path: (
                    path.suffix.lower() != ".mxl",
                    str(path),
                ),
            )

        print("[AUDIVERIS] File hasil:")
        for path in root.rglob("*"):
            if path.is_file():
                print(f" - {path.relative_to(root)}")

        # Simpan semua file hasil Audiveris sebelum TemporaryDirectory dihapus.
        # Folder terbaru selalu tersedia di backend/debug_audiveris/latest.
        debug_run = DEBUG_DIR / "latest"
        if debug_run.exists():
            shutil.rmtree(debug_run)
        debug_run.mkdir(parents=True, exist_ok=True)

        for path in root.rglob("*"):
            if path.is_file():
                destination = debug_run / path.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

        print(f"[DEBUG] Hasil Audiveris disalin ke: {debug_run.resolve()}")

        if process.returncode != 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Audiveris gagal membaca partitur. "
                    "Lihat log terminal backend untuk detail."
                ),
            )
        if not candidates:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Audiveris tidak menghasilkan MusicXML/MXL. "
                    "Coba gunakan PDF yang lebih jelas."
                ),
            )

        musicxml_path = candidates[0]
        print(f"[MUSICXML] Parsing: {musicxml_path.name}")

        try:
            score = converter.parse(str(musicxml_path))
            result_data = convert_score(score)
        except Exception as exc:
            print("[ERROR] Gagal membaca hasil MusicXML:")
            traceback.print_exc()
            raise HTTPException(
                status_code=422,
                detail=f"MusicXML gagal diproses: {type(exc).__name__}: {exc}",
            )

        print(f"[CONVERT] Berhasil. Total event: {result_data['totalEvents']}")

        # Create a PDF overlay from notehead coordinates in Audiveris' OMR.
        omr_candidates = sorted(debug_run.rglob("*.omr"))
        if not omr_candidates:
            raise HTTPException(
                status_code=422,
                detail="MusicXML terbaca, tetapi file OMR untuk koordinat not tidak ditemukan.",
            )

        output_name = f"annotated-{uuid.uuid4().hex}.pdf"
        output_path = OUTPUT_DIR / output_name
        try:
            detected_key = detect_key(score)
            # Fallback key-signature count from MusicXML. For example, D major
            # has two sharps, so F in the score should be treated as F-sharp.
            fallback_key_fifths = int(getattr(detected_key, "sharps", 0) or 0)
            print(
                f"[ANNOTATION KEY] detected={detected_key}; "
                f"fallback fifths={fallback_key_fifths}"
            )
            annotated_count = create_annotated_pdf(
                source_path=source,
                suffix=suffix,
                omr_path=omr_candidates[0],
                score=score,
                tonic=detected_key.tonic,
                output_path=output_path,
                fallback_key_fifths=fallback_key_fifths,
            )
        except Exception as exc:
            print("[PDF ANNOTATION] Gagal membuat PDF:")
            traceback.print_exc()
            raise HTTPException(
                status_code=422,
                detail=(
                    f"PDF anotasi gagal dibuat ({type(exc).__name__}): {exc}. "
                    "Lihat detail traceback di terminal backend."
                ),
            )

        return {
            "ok": True,
            "filename": filename,
            **result_data,
            "coordinates_available": True,
            "annotated_noteheads": annotated_count,
            "annotation_accuracy": "audiveris_staff_pitch_with_staff_id_to_clef_map_same_degrees_both_hands",
            "coordinate_message": (
                "Angka dihitung dari posisi kepala not dan clef tiap staff yang dicocokkan "
                "melalui ID staff Audiveris. Kedua tangan memakai pemetaan angka yang sama; "
                "oktaf ditandai dengan titik, bukan dengan menggeser derajat angka pada bass. "
                "Tetap periksa hasil visual karena kesalahan OMR dapat memengaruhi pembacaan simbol."
            ),
            "download_url": f"/api/download/{output_name}",
        }


@app.get("/api/download/{filename}")
def download_annotated_pdf(filename: str):
    # Prevent path traversal: only allow generated output basenames.
    if Path(filename).name != filename or not filename.startswith("annotated-") or not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Nama file tidak valid.")
    path = OUTPUT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File PDF tidak ditemukan atau sudah dihapus.")
    return FileResponse(path, media_type="application/pdf", filename="sheet-to-number.pdf")
