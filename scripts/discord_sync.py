#!/usr/bin/env python3
"""Publica los documentos de docs/ en un canal Foro de Discord, legibles dentro de Discord.

- Cada documento es una publicación (hilo) del foro.
- Convierte el Markdown a lo que Discord muestra: sin tablas (pasan a listas), sin líneas
  horizontales, sin enlaces relativos, y lo parte en mensajes de menos de 2000 caracteres.
- Guarda en .discord/estado.json los IDs de hilos y mensajes para EDITARLOS en las siguientes
  actualizaciones en lugar de duplicarlos.

Uso:
  DISCORD_WEBHOOK=... python3 scripts/discord_sync.py          # publicar/actualizar
  python3 scripts/discord_sync.py --dry-run                     # solo mostrar los mensajes
  python3 scripts/discord_sync.py --nuevo                       # ignorar estado y crear todo de nuevo
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ESTADO = RAIZ / ".discord" / "estado.json"
LIMITE = 1900

# Orden y título de cada publicación en el foro.
DOCUMENTOS = [
    ("docs/guia-primeros-pasos.md", "1 · Primeros 10 pasos"),
    ("docs/resumen-reglas.md", "2 · Resumen del juego"),
    ("docs/resumen-foro.md", "3 · Lo que dice el foro"),
]


# ---------- Conversión de Markdown a formato Discord ----------

def celdas(linea):
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def tabla_a_lista(filas):
    cab = celdas(filas[0])
    salida = []
    for f in filas[2:]:  # filas[1] es el separador |---|
        c = celdas(f)
        if not any(c):
            continue
        etiqueta = f"{cab[0]} {c[0]}" if re.match(r"^[\d–-]+$", c[0]) else c[0]
        primero = f"**{etiqueta}**" if not etiqueta.startswith("**") else etiqueta
        if len(cab) == 2:
            resto = [c[1]] if len(c) > 1 and c[1] else []
        else:
            resto = [f"{cab[i]}: {v}" for i, v in enumerate(c[1:], 1) if v and i < len(cab)]
        salida.append(f"- {primero}" + (" — " + " · ".join(resto) if resto else ""))
    return salida


def convertir(texto):
    lineas = texto.splitlines()
    out, i = [], 0
    while i < len(lineas):
        l = lineas[i]
        if l.lstrip().startswith("|") and i + 1 < len(lineas) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lineas[i + 1]):
            j = i
            while j < len(lineas) and lineas[j].lstrip().startswith("|"):
                j += 1
            out.extend(tabla_a_lista(lineas[i:j]))
            i = j
            continue
        if re.match(r"^\s*---+\s*$", l):  # línea horizontal: Discord no la muestra
            i += 1
            continue
        l = re.sub(r"^#{4,}\s", "### ", l)  # Discord solo soporta #, ## y ###
        l = re.sub(r"^(\s*)- \[ \] ", r"\1- ☐ ", l)
        l = re.sub(r"^(\s*)- \[x\] ", r"\1- ☑ ", l, flags=re.I)
        # Enlaces a otros documentos del repo -> texto en negrita (en Discord son otras publicaciones)
        l = re.sub(r"\[([^\]]+)\]\((?!https?://)[^)]+\)", r"**\1**", l)
        l = l.replace("****", "")
        l = l.replace("\\*", "*")
        out.append(l)
        i += 1
    txt = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def partir(texto):
    """Parte por secciones (##) y, si hace falta, por párrafos y líneas."""
    secciones = re.split(r"\n(?=## )", texto)
    piezas = []
    for s in secciones:
        if len(s) <= LIMITE:
            piezas.append(s)
            continue
        actual = ""
        for bloque in s.split("\n\n"):
            for parte in ([bloque] if len(bloque) <= LIMITE else bloque.split("\n")):
                sep = "\n\n" if bloque == parte else "\n"
                if len(actual) + len(sep) + len(parte) > LIMITE and actual:
                    piezas.append(actual)
                    actual = parte
                else:
                    actual = f"{actual}{sep}{parte}" if actual else parte
        if actual:
            piezas.append(actual)
    # Juntar piezas chicas consecutivas
    mensajes = []
    for p in piezas:
        p = p.strip()
        if mensajes and len(mensajes[-1]) + 2 + len(p) <= LIMITE:
            mensajes[-1] += "\n\n" + p
        else:
            mensajes.append(p)
    return mensajes


# ---------- Discord ----------

def pedir(metodo, url, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    for intento in range(5):
        req = urllib.request.Request(url, data=datos, method=metodo,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "basketpc-sync"})
        try:
            with urllib.request.urlopen(req) as r:
                time.sleep(0.6)
                cuerpo_r = r.read()
                return json.loads(cuerpo_r) if cuerpo_r else None
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            if e.code == 429:
                time.sleep(float(json.loads(txt).get("retry_after", 2)) + 0.5)
                continue
            if e.code == 404 and metodo in ("PATCH", "DELETE"):
                return "404"
            if "220003" in txt:
                sys.exit("El webhook apunta a un canal de texto. Movelo a un canal de tipo FORO "
                         "(Discord > Ajustes del servidor > Integraciones > Webhooks) y volvé a correr.")
            raise RuntimeError(f"{metodo} -> {e.code}: {txt}") from None
    raise RuntimeError("Demasiados reintentos por límite de Discord")


def publicar(webhook, nuevo):
    estado = {} if nuevo or not ESTADO.exists() else json.loads(ESTADO.read_text())
    sin_embeds = 4  # no mostrar vista previa de los links

    for ruta, titulo in DOCUMENTOS:
        mensajes = partir(convertir((RAIZ / ruta).read_text()))
        previo = estado.get(ruta)

        if previo:
            hilo, ids = previo["hilo"], previo["mensajes"]
            # Si el hilo o su primer mensaje ya no existen, se recrea
            if pedir("PATCH", f"{webhook}/messages/{ids[0]}?thread_id={hilo}",
                     {"content": mensajes[0], "flags": sin_embeds}) == "404":
                previo = None
            else:
                nuevos_ids = [ids[0]]
                for k, m in enumerate(mensajes[1:], 1):
                    if k < len(ids):
                        pedir("PATCH", f"{webhook}/messages/{ids[k]}?thread_id={hilo}",
                              {"content": m, "flags": sin_embeds})
                        nuevos_ids.append(ids[k])
                    else:
                        r = pedir("POST", f"{webhook}?wait=true&thread_id={hilo}",
                                  {"content": m, "flags": sin_embeds})
                        nuevos_ids.append(r["id"])
                for sobra in ids[len(mensajes):]:
                    pedir("DELETE", f"{webhook}/messages/{sobra}?thread_id={hilo}")
                estado[ruta] = {"hilo": hilo, "mensajes": nuevos_ids}
                print(f"Actualizado: {titulo} ({len(mensajes)} mensajes)")

        if not previo:
            r = pedir("POST", f"{webhook}?wait=true",
                      {"content": mensajes[0], "thread_name": titulo, "flags": sin_embeds})
            hilo = r["channel_id"]
            ids = [r["id"]]
            for m in mensajes[1:]:
                ids.append(pedir("POST", f"{webhook}?wait=true&thread_id={hilo}",
                                 {"content": m, "flags": sin_embeds})["id"])
            estado[ruta] = {"hilo": hilo, "mensajes": ids}
            print(f"Publicado: {titulo} ({len(mensajes)} mensajes)")

    ESTADO.parent.mkdir(exist_ok=True)
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False) + "\n")


def main():
    args = sys.argv[1:]
    if "--dry-run" in args:
        for ruta, titulo in DOCUMENTOS:
            msgs = partir(convertir((RAIZ / ruta).read_text()))
            print(f"\n==================== {titulo}: {len(msgs)} mensajes "
                  f"(máx {max(map(len, msgs))} caracteres)")
            for k, m in enumerate(msgs, 1):
                print(f"\n----- mensaje {k} ({len(m)}) -----\n{m}")
        return
    webhook = os.environ.get("DISCORD_WEBHOOK", "").split("?")[0].rstrip("/")
    if not webhook:
        sys.exit("Falta DISCORD_WEBHOOK")
    publicar(webhook, "--nuevo" in args)


if __name__ == "__main__":
    main()
