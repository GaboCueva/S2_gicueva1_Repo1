import serial
import serial.tools.list_ports
import sqlite3
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, session, redirect
import threading
import time
from queue import Queue

def requiere_login(tipo=None):
    if not session.get("login"):
        return False

    if tipo and session.get("tipo") != tipo:
        return False

    return True

INTERES_ANUAL = 0.01  # aca se puede cambiar el interés, variable entre (0 - 0.05)
SEMANAS = 40

INTERES_SEMANAL = INTERES_ANUAL / SEMANAS

MODO_TEST = True
# ==============================
# deteccion de arduino
# ==============================
cola_uid = Queue()

def detectar_arduino():
    puertos = serial.tools.list_ports.comports()
    for p in puertos:
        if "Arduino" in p.description or "CH340" in p.description:
            return p.device
    return None

try:
    puerto = detectar_arduino()
    arduino = serial.Serial(puerto, 9600, timeout=1) if puerto else None
except Exception as e:
    print("Error conectando Arduino:", e)
    arduino = None

# ==============================
# database
# ==============================
# Conexion con lock para evitar 'Recursive use of cursors' en multihilo
import threading as _threading
_db_lock = _threading.Lock()

def get_db():
    """Retorna una conexion y cursor frescos para el request actual."""
    c = sqlite3.connect('rfid.db', check_same_thread=False)
    c.row_factory = None
    return c, c.cursor()

# Conexion global para CREATE TABLE del startup y para el hilo RFID
conn = sqlite3.connect('rfid.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS tarjetas (
    uid TEXT PRIMARY KEY,
    nombre TEXT,
    saldo REAL DEFAULT 0,
    bloqueada INTEGER DEFAULT 0
)
''')

# Migración: agregar columna bloqueada si no existe (para bases de datos existentes)
try:
    cursor.execute("ALTER TABLE tarjetas ADD COLUMN bloqueada INTEGER DEFAULT 0")
    conn.commit()
except:
    pass  # La columna ya existe

cursor.execute('''
CREATE TABLE IF NOT EXISTS usuarios (
    uid TEXT PRIMARY KEY,
    nombres TEXT,
    apellidos TEXT,
    correo TEXT,
    telefono TEXT,
    institucion TEXT,
    curso TEXT,
    fecha_registro TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS ahorros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    semana TEXT,
    monto REAL,
    tipo TEXT CHECK(tipo IN ('ahorro','interes'))
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS configuracion_ahorro (
    uid TEXT PRIMARY KEY,
    modo TEXT,
    valor REAL
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS metas_ahorro (
    uid TEXT PRIMARY KEY,
    meta REAL DEFAULT 0
)
''')

cursor.execute("""
CREATE TABLE IF NOT EXISTS historial (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    movimiento TEXT,
    pago REAL,
    fecha TEXT,
    detalle TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS puntos (
    uid TEXT PRIMARY KEY,
    puntos REAL DEFAULT 0
)
""")
conn.commit()

# ==============================
# productos
# ==============================
productos = [
    {"nombre": "Forestea", "precio": 1.00},
    {"nombre": "Guata", "precio": 1.50},
    {"nombre": "Horchata", "precio": 0.75},
    {"nombre": "Cola", "precio": 1.00},
    {"nombre": "Agua", "precio": 0.75},
    {"nombre": "Papas", "precio": 1.25},
    {"nombre": "Chicle", "precio": 0.50},
    {"nombre": "Galletas", "precio": 0.80},
    {"nombre": "Chocolate", "precio": 1.20},
    {"nombre": "Jugo", "precio": 0.90},
    {"nombre": "Empanada", "precio": 1.50}
]

# ==============================
# mostrar bien LCD
# ==============================
def formatear_nombre_lcd(nombre):
    partes = nombre.strip().split()

    if len(partes) < 2:
        return nombre

    # 👉 Detectar formato: APELLIDOS NOMBRES
    # Asumimos: primeros 2 = apellidos, resto = nombres

    if len(partes) >= 3:
        primer_apellido = partes[0]
        primer_nombre = partes[2]
    else:
        # fallback simple
        primer_nombre = partes[0]
        primer_apellido = partes[1]

    return f"{primer_nombre} {primer_apellido[0]}"

# ==============================
# variables
# ==============================
cola_uid = Queue()
venta_data = {}
modo_recarga = False
uid_recarga = None

# ==============================
# usuarios admins
# ==============================
USUARIOS = {
    "cueva": "jijon",
    "arias": "celi",
    "carrion": "gonzalez",
    "villamagua": "camacho",
    "medina": "carrion",
    "alejandro": "ruiz",
    "ortega": "kirby"
}

app = Flask(__name__)
app.secret_key = "barpay_secret_key"

# Set para rastrear UIDs con credenciales invalidadas
_sesiones_invalidadas = set()

# ==============================
# HTML
# ==============================
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Login</title>

<style>
body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: url('/static/header.png') no-repeat center center fixed;
    background-size: cover;
}

.overlay {
    position: fixed;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.6);
    display: flex;
    justify-content: center;
    align-items: center;
}

.login-card {
    background: white;
    padding: 40px;
    border-radius: 15px;
    width: 320px;
    box-shadow: 0px 10px 30px rgba(0,0,0,0.3);
    text-align: center;

    animation: fadeIn 0.6s ease;
}

@keyframes fadeIn {
    from {
        opacity: 0;
        transform: translateY(30px);
    }
    to {
        opacity: 1;
        transform: translateY(0);
    }
}

.login-card h2 {
    margin-bottom: 20px;
}

.login-card input {
    width: 100%;
    padding: 12px;
    margin: 10px 0;
    border: none;
    border-radius: 8px;
    background: #f1f1f1;
}

.login-card button {
    width: 100%;
    padding: 12px;
    border: none;
    border-radius: 8px;
    background: #007BFF;
    color: white;
    cursor: pointer;
}

.login-card button:hover {
    background: #0056b3;
}

.login-card select {
    width: 100%;
    padding: 12px;
    margin: 10px 0;
    border-radius: 8px;
    background: #f1f1f1;
    border: none;
}

/* 🔴 ERROR */
.error {
    color: red;
    margin-top: 10px;
    font-weight: bold;
}
</style>
</head>

<body>

<div class="overlay">
    <div class="login-card">
        <h2>🔐 Iniciar Sesión</h2>

        <form method="POST">
            <input type="text" name="user" placeholder="Usuario" required>
            <input type="password" name="pass" placeholder="Contraseña" required>
            <select name="tipo" required>
                <option value="">Tipo de acceso</option>
                <option value="admin">Administrador</option>
                <option value="cliente">Estudiante</option>
            </select>

            <button type="submit">Ingresar</button>
        </form>

        {% if error %}
        <div class="error">{{error}}</div>
        {% endif %}
    </div>
</div>

</body>
</html>
"""
CLIENTE_HTML = """
<!DOCTYPE html>
<html>
<head><script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<title>BarPay - Cliente</title>

<style>
body {
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    transition: background-image 0.5s ease-in-out;
}

body.inicio-bg::after {
    content: '';
    position: fixed;
    top: 50px;
    left: 200px;
    right: 0;
    bottom: 0;
    background-image: url('/static/inicio.png');
    background-size: cover;
    background-position: center;
    z-index: 0;
    pointer-events: none;
}

body.app-bg {
    background-image: url('/static/app.png');
}

/* HEADER */
.header {
    background: #0d6efd;
    color: white;
    padding: 15px;
    font-size: 20px;

    position: fixed;
    width: 100%;
    left: 0px;
    top: 0;
    z-index: 1000;
}

/* SIDEBAR */
.sidebar {
    position: fixed;
    top: 50px;
    left: 0px;
    width: 200px;
    height: 100%;
    background: #222;
    padding-top: 20px;
}

.sidebar button {
    width: 100%;
    padding: 15px;
    background: none;
    border: none;
    color: white;
    text-align: left;
    cursor: pointer;
}

.sidebar button:hover {
    background: #444;
}

/* CONTENIDO */
.content {
    margin-left: 220px;
    padding: 80px 20px 20px 20px;
    width: calc(100% - 220px); /* 🔥 evita que se meta debajo del sidebar */
    box-sizing: border-box;
}

.card {
    background: transparent;
    padding: 20px;
    margin-bottom: 20px;
    color: black;      
}

table {
    width: 100%;
    border-collapse: collapse;
}

td, th {
    border: 1px solid #ccc;
    padding: 8px;
}

.gridGraficas {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
}

.cardGrafica {
    background: white;
    padding: 15px;
    border-radius: 10px;
    height: 300px;
}

canvas {
    width: 100% !important;
    height: 220px !important;
}
</style>

<script>
let saldo;
let modo;
let bloqueadoReal = 0;
document.addEventListener("DOMContentLoaded", function(){

    const slider = document.getElementById("sliderAhorro");
    const inputMonto = document.getElementById("inputMonto");

    const modoPorcentajeDiv = document.getElementById("modoPorcentaje");
    const modoMontoDiv = document.getElementById("modoMonto");

    const radios = document.querySelectorAll('input[name="modoAhorro"]');

    saldo = {{saldo}};
    modo = "{{modo_conf}}";

    if(modo === "porcentaje"){
        bloqueadoReal = saldo * ({{porcentaje_conf}} / 100);
    } else {
        bloqueadoReal = {{monto_conf}};
    }

    slider.addEventListener("input", actualizarTodo);
    inputMonto.addEventListener("input", actualizarTodo);

    document.getElementById("inputMeta").addEventListener("input", actualizarMeta);

    // Mostrar el div correcto según el modo guardado
    if(modo === "porcentaje"){
        modoPorcentajeDiv.style.display = "block";
        modoMontoDiv.style.display = "none";
    } else {
        modoPorcentajeDiv.style.display = "none";
        modoMontoDiv.style.display = "block";
    }

    radios.forEach(radio => {
        radio.addEventListener("change", function(){
            modo = this.value;

            modoPorcentajeDiv.style.display = (modo === "porcentaje") ? "block":"none";
            modoMontoDiv.style.display = (modo === "monto") ? "block":"none";

            actualizarTodo();
        });
    });

    actualizarTodo();
    actualizarMeta();
    mostrar("inicio");
});

function mostrar(seccion){

    const secciones = ["inicio", "ahorro", "historial", "estadisticas"];

    secciones.forEach(id => {
        let el = document.getElementById(id);
        if(el) el.style.display = "none";
    });

    document.getElementById(seccion).style.display = "block";

    // 🎨 fondo
    if(seccion === "inicio"){
        document.body.className = "inicio-bg";
    } else {
        document.body.className = "app-bg";
    }

    // 🔥 cargar gráficas SOLO cuando entras
    if(seccion === "estadisticas"){
        cargarGraficas();
        cargarRanking(); // 🔥 importante
    }
}

function actualizarTodo(){

    let slider = document.getElementById("sliderAhorro");
    let inputMonto = document.getElementById("inputMonto");

    let textoPorcentaje = document.getElementById("valorAhorro");
    let textoMonto = document.getElementById("valorMonto");

    let bloqueado = 0;

    if(modo === "porcentaje"){
        let porcentaje = parseFloat(slider.value);
        bloqueado = saldo * (porcentaje / 100);
        textoPorcentaje.innerText = porcentaje + "% ($" + bloqueado.toFixed(2) + ")";
    } else {
        bloqueado = parseFloat(inputMonto.value) || 0;
    }

    if(bloqueado > saldo){
        bloqueado = saldo;
        inputMonto.value = saldo.toFixed(2);
    }

    textoMonto.innerText = "$" + bloqueado.toFixed(2);

    document.getElementById("saldoDisponible").innerText =
        (saldo - bloqueadoReal).toFixed(2);

    document.getElementById("ahorroBloqueado").innerText =
        bloqueadoReal.toFixed(2);
}

function actualizarMeta(){

    let ahorrado = parseFloat(document.getElementById("ahorradoActual").innerText);
    let meta = parseFloat(document.getElementById("inputMeta").value) || 0;

    let porcentaje = meta > 0 ? (ahorrado / meta) * 100 : 0;

    if(porcentaje > 100) porcentaje = 100;

    document.getElementById("barraProgreso").style.width = porcentaje + "%";
    document.getElementById("porcentajeMeta").innerText = porcentaje.toFixed(1) + "%";
}

function guardarMeta(){

    let meta = parseFloat(document.getElementById("inputMeta").value);

    fetch("/guardar_meta", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({ meta: meta })
    })
    .then(res => res.json())
    .then(data => {
        alert(data.mensaje);
        actualizarMeta();
    })
    .catch(err => {
        console.error(err);
        alert("❌ Error al guardar meta");
    });
}

function guardarConfiguracion(){

    let modoSeleccionado = document.querySelector('input[name="modoAhorro"]:checked').value;

    let valor;

    if(modoSeleccionado === "porcentaje"){
        valor = parseFloat(document.getElementById("sliderAhorro").value);
    } else {
        valor = parseFloat(document.getElementById("inputMonto").value);
    }

    fetch("/guardar_ahorro", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            modo: modoSeleccionado,
            valor: valor
        })
    })
    .then(res => res.json())
    .then(data => {

        // 🔥 actualizar valor real SOLO aquí
        if(modoSeleccionado === "porcentaje"){
            bloqueadoReal = saldo * (valor / 100);
        } else {
            bloqueadoReal = valor;
        }

        actualizarTodo();
	actualizarMeta();

        alert(data.mensaje);
    })
    .catch(err => {
        console.error(err);
        alert("❌ Error al guardar");
    });
}

let graficaAhorro;
let graficaGastos;
let graficaProyeccion;

function filtrarHistorial(){

    let filtro = document.getElementById("buscarHistorial").value;

    document.querySelectorAll("#historial table tr").forEach((fila, i)=>{

        if(i === 0) return; // header

        let id = fila.children[0].innerText.replace("#","");

        fila.style.display = id.includes(filtro) ? "" : "none";
    });
}

function cargarGraficas(){

    fetch("/datos_ahorro")
    .then(res => res.json())
    .then(data => {

        // -------------------------
        // AHORRO REAL
        // -------------------------
        let labelsAhorro = data.ahorro.map(x => x[0]);
        let valoresAhorro = data.ahorro.map(x => x[1]);

        // -------------------------
        // PROYECCIÓN INTELIGENTE (tendencia real)
        // -------------------------
        let labelsProy = [...labelsAhorro];
        let proyeccion = [...valoresAhorro];

        if(valoresAhorro.length > 2){

            let crecimiento = [];

            for(let i = 1; i < valoresAhorro.length; i++){
                crecimiento.push(valoresAhorro[i] - valoresAhorro[i-1]);
            }

            let promedioCrecimiento = crecimiento.reduce((a,b)=>a+b,0) / crecimiento.length;

            let ultimo = valoresAhorro[valoresAhorro.length - 1];

            for(let i=0; i<3; i++){
                ultimo += promedioCrecimiento;
                proyeccion.push(ultimo);
                labelsProy.push("Proy " + (i+1));
            }
        }

        // CONFIGURACIÓN DE EJES (🔥 mejora visual)
        const opcionesEjes = {
            scales: {
                x: {
                    ticks: { color: "#000" },
                    grid: { color: "rgba(0,0,0,0.1)" }
                },
                y: {
                    ticks: { color: "#000" },
                    grid: { color: "rgba(0,0,0,0.1)" }
                }
            },
            plugins: {
                legend: {
                    labels: { color: "#000" }
                }
            },
            responsive: true,
            maintainAspectRatio: false
        };

        // -------------------------
        // GRÁFICA AHORRO
        // -------------------------
        if(graficaAhorro){
            graficaAhorro.destroy();
        }

        graficaAhorro = new Chart(document.getElementById("graficaAhorro"), {
            type: "line",
            data: {
                labels: labelsAhorro,
                datasets: [{
                    label: "Ahorro real",
                    data: valoresAhorro,
                    tension: 0.3
                }]
            },
            options: opcionesEjes
        });

        // -------------------------
        // GRÁFICA PROYECCIÓN
        // -------------------------
        if(graficaProyeccion){
            graficaProyeccion.destroy();
        }

        graficaProyeccion = new Chart(document.getElementById("graficaProyeccion"), {
            type: "line",
            data: {
                labels: labelsProy,
                datasets: [{
                    label: "Tendencia de ahorro",
                    data: proyeccion,
                    borderDash: [6,6],
                    tension: 0.3
                }]
            },
            options: opcionesEjes
        });

        // -------------------------
        // GASTOS
        // -------------------------
        let labelsGastos = data.gastos.map(x => x[0]);
        let valoresGastos = data.gastos.map(x => x[1]);

        if(graficaGastos){
            graficaGastos.destroy();
        }

        graficaGastos = new Chart(document.getElementById("graficaGastos"), {
            type: "bar",
            data: {
                labels: labelsGastos,
                datasets: [{
                    label: "Gastos recientes",
                    data: valoresGastos
                }]
            },
            options: opcionesEjes
        });

    })
    .catch(err => {
        console.error(err);
        alert("❌ Error cargando estadísticas");
    });
}

function cargarRanking(){
    let uid = "{{uid}}";

    fetch("/ranking?uid=" + uid)
    .then(res => res.json())
    .then(data => {
        let tabla = document.getElementById("tablaRanking");
        if(!tabla) return;

        let top5 = data.top5 || [];
        let yo = data.yo || null;

        if(top5.length === 0){
            tabla.innerHTML = "<tr><td colspan='3'>Sin datos</td></tr>";
            return;
        }

        let html = "";

        top5.forEach((r, i) => {
            let medal = "";
            if(i === 0) medal = "🥇";
            else if(i === 1) medal = "🥈";
            else if(i === 2) medal = "🥉";

            let esYo = (r.uid === uid);
            let estilo = esYo ? "background:#d4edda; font-weight:bold;" : "";

            html += `
                <tr style="${estilo}">
                    <td>${medal} ${r.posicion}</td>
                    <td>${r.nombre}${esYo ? " 👈 tú" : ""}</td>
                    <td>${Number(r.puntos).toFixed(2)}</td>
                </tr>
            `;
        });

        // Si el usuario no está en el top 5, mostrar su posición separada
        if(yo){
            html += `
                <tr><td colspan="3" style="text-align:center; color:#888;">···</td></tr>
                <tr style="background:#d4edda; font-weight:bold;">
                    <td>${yo.posicion}</td>
                    <td>${yo.nombre} 👈 tú</td>
                    <td>${Number(yo.puntos).toFixed(2)}</td>
                </tr>
            `;
        }

        tabla.innerHTML = html;
    })
    .catch(err => {
        console.error(err);
        alert("❌ Error cargando ranking");
    });
}
</script>

</head>

<body>

<div class="header">
    <img src="/static/logo.png" height="40" style="vertical-align:middle;">
    <span style="margin-left:10px;">BarPay - Bienvenido {{nombre}}</span>
</div>

<div class="sidebar">
    <button onclick="mostrar('inicio')">🏠 Inicio</button>
    <button onclick="mostrar('ahorro')">💰 Ahorro</button>
    <button onclick="mostrar('historial')">📜 Historial</button>
    <button onclick="mostrar('estadisticas')">📊 Estadísticas</button>
    <button onclick="location.href='/logout'">🚪 Cerrar sesión</button>
</div>

<div class="content">

    <div id="inicio"></div>

    <!-- AHORRO -->
    <div id="ahorro">
        <div class="card">
            <h2>💳 Saldo total</h2>
            <h1>$ {{saldo}}</h1>

            <h2>🏦 Ahorro total (incluye intereses)</h2>
            <h1>$ {{ahorro_total}}</h1>

            <p id="mensajePuntos">
                {% if ahorro_total < 50 %}
                    No generas intereses aún (mínimo $50 ahorrados requerido).<br>
                {% elif puntos_generados > 0 %}
                    Esta semana generaste {{ "%.2f"|format(puntos_generados) }} puntos. <br>
                {% else %}
                    Esta semana no generaste puntos.<br>
                {% endif %}

                Puntos totales: {{puntos_totales}}
            </p>
	    
            <p>
            🏆 Actualmente estás en el puesto <b>#{{posicion}}</b> de {{total_usuarios}} estudiantes.<br>
            Sigue ahorrando para escalar posiciones 🚀
            </p>

	    <p>💰 Saldo disponible: $<span id="saldoDisponible">{{saldo}}</span></p>
	    <p>🔒 Ahorro bloqueado (esta semana): $<span id="ahorroBloqueado">0.00</span></p>

            <h3>🎯 Meta de ahorro</h3>

            <input type="number" id="inputMeta" value="{{meta_ahorro}}" step="0.01" min="0">
            <button onclick="guardarMeta()">💾 Guardar meta</button>

            <p>Ahorrado: $<span id="ahorradoActual">{{ahorro_total}}</span></p>

            <div style="width: 100%; background: #ddd; border-radius: 10px; overflow: hidden; height: 20px;">
                <div id="barraProgreso" style="height: 100%; width: 0%; background: green;"></div>
            </div>

            <p><span id="porcentajeMeta">0%</span> completado</p>

            <h3>📊 Configurar ahorro semanal</h3>

            <label>
                <input type="radio" name="modoAhorro" value="porcentaje" {{'checked' if modo_conf=='porcentaje' else ''}}>
                Por porcentaje
            </label>

            <label>
                <input type="radio" name="modoAhorro" value="monto" {{'checked' if modo_conf=='monto' else ''}}>
                Monto fijo
            </label>

            <br><br>

            <!-- PORCENTAJE -->
            <div id="modoPorcentaje" style="display:none;">
                <input type="range" min="0" max="100" value="{{porcentaje_conf}}" id="sliderAhorro">
                <p>Porcentaje: <span id="valorAhorro"></span></p>
            </div>

            <!-- MONTO -->
            <div id="modoMonto" style="display:none;">
                <input type="number" id="inputMonto" value="{{monto_conf}}" step="0.01" min="50">
                <p>Ahorro: <span id="valorMonto">$0.00</span></p>
            </div>
	    <button onclick="guardarConfiguracion()">💾 Guardar Configuración</button>
        </div>
    </div>

    <!-- HISTORIAL DEL ESTUDIANTE -->
    <div id="historial" class="seccion" style="display:none;">
        <h2>Historial</h2>

        <input type="number" id="buscarHistorial" placeholder="Buscar por ID..." onkeyup="filtrarHistorial()">

        <table border="1">
            <tr>
                <th>ID</th>
                <th>Pago</th>
                <th>Detalle</th>
                <th>Movimiento</th>
                <th>Fecha</th>
            </tr>

            {% for h in historial %}
            <tr>
                <!-- ID -->
                <td><b>#{{ h[0] }}</b></td>

                <!-- Pago con formato -->
                <td>
                {% if h[2] == 'compra' %}
                    -${{ "%.2f"|format(h[3]) }}
                {% elif h[2] == 'recarga' %}
                    +${{ "%.2f"|format(h[3]) }}
                {% else %}
                    +${{ "%.2f"|format(h[3]) }}
                {% endif %}
                </td>

                <!-- Detalle -->
                <td>{{ h[5] or '-' }}</td>

                <!-- Movimiento -->
                <td>{{ h[2] }}</td>

                <!-- Fecha -->
                <td>{{ h[4] }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>

    <!-- ESTADISTICAS -->
    <div id="estadisticas" style="display:none">

        <h2>📊 Estadísticas</h2>

        <div class="gridGraficas">

            <div class="cardGrafica">
                <h3>💰 Ahorro</h3>
                <canvas id="graficaAhorro"></canvas>
            </div>

            <div class="cardGrafica">
                <h3>💸 Gastos</h3>
                <canvas id="graficaGastos"></canvas>
            </div>

            <div class="cardGrafica">
                <h3>🏆 Ranking</h3>
                <table>
                    <thead>
                        <tr>
                            <th>#</th>
                            <th>Nombre</th>
                            <th>Puntos</th>
                        </tr>
                    </thead>
                    <tbody id="tablaRanking"></tbody>
                </table>
            </div>

        </div>

        <div class="cardGrafica" style="margin-top:20px;">
            <h3>📈 Proyección</h3>
            <canvas id="graficaProyeccion"></canvas>
        </div>

    </div>

</html>
"""
HTML = """
<!DOCTYPE html>
<html>
<head>
<title>BarPay</title>

<style>
/* FONDO GENERAL */
body {
    margin:0;
    font-family: Arial, sans-serif;
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    transition: background 0.5s ease;
}

/* 🏠 INICIO */
body.inicio-bg::after {
    content: '';
    position: fixed;
    top: 70px;
    left: 220px;
    right: 0;
    bottom: 0;
    background-image: url('/static/inicio.png');
    background-size: cover;
    background-position: center;
    z-index: 0;
    pointer-events: none;
}

/* 📊 APP */
body.app-bg {
    background-image: url('/static/app.png');
}

/* HEADER */
.header {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 70px;
    
    background: #0d6efd;
    color: white;

    display: flex;
    align-items: center;
    padding-left: 20px;

    z-index: 1000;
}

.logo {
    display:flex;
    align-items:center;
    gap:10px;
    font-size:20px;
    font-weight:bold;
}

/* SIDEBAR */
.sidebar {
    position: fixed;
    top: 70px;
    left: 0;
    width: 220px;
    height: calc(100% - 70px);

    background: #1e1e2f;
    z-index: 900;
}

.sidebar button {
    width: 100%;
    padding: 15px;
    background: none;
    border: none;
    color: #ffffff;
    text-align: left;
    cursor: pointer;
    font-size: 15px;
    transition: 0.2s;
}

.sidebar button:hover {
    background: #343454;
}

.sidebar button.active {
    background: #0d6efd;
}

/* MAIN */
.main {
    margin-left:240px;
    margin-top: 70px;
    padding:20px;
}

.content {
    margin-left: 220px;
    margin-top: 70px;
    padding: 20px;
}

/* PRODUCTOS */
.producto {
    margin:5px;
}

/* CENTRAR */
.center {
    display:flex;
    justify-content:center;
    align-items:center;
    height:70vh;
    flex-direction:column;
}

#ranking table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 15px;
}

#ranking th, #ranking td {
    padding: 10px;
    text-align: center;
}

#ranking tr:nth-child(even) {
    background: #2a2a40;
}

#ranking tr:hover {
    background: #3a3a55;
}
</style>

<script>
// =============================
// navegacion
// =============================
function mostrarSeccion(id){

    document.querySelectorAll(".seccion").forEach(s => s.style.display = "none");
    document.getElementById(id).style.display = "block";

    // 🔥 CAMBIO DE FONDO
    if(id === "inicio"){
        document.body.classList.add("inicio-bg");
        document.body.classList.remove("app-bg");
    } else {
        document.body.classList.add("app-bg");
        document.body.classList.remove("inicio-bg");
    }

    if (id === "ranking") {
        cargarRanking();
    }
}

// =============================
// js
// =============================
function filtrarProductos(){
    let filtro = document.getElementById("buscador").value.toLowerCase();
    document.querySelectorAll(".producto").forEach(p=>{
        let nombre = p.dataset.nombre.toLowerCase();
        p.style.display = nombre.includes(filtro) ? "block" : "none";
    });
}

function mostrar(seccion){

    // ocultar todo
    document.getElementById("inicio").style.display = "none";
    document.getElementById("ahorro").style.display = "none";
    document.getElementById("historial").style.display = "none";

    // mostrar sección
    document.getElementById(seccion).style.display = "block";

    // 🎨 cambiar fondo dinámicamente
    if(seccion === "inicio"){
        document.body.className = "inicio-bg";
    } else {
        document.body.className = "app-bg";
    }
}

function filtrarProductos(){
    let filtro = document.getElementById("buscador").value.toLowerCase();

    let productos = Array.from(document.querySelectorAll(".producto"));

    productos.sort((a,b)=>{
        return a.dataset.nombre.localeCompare(b.dataset.nombre);
    });

    productos.forEach(p=>{
        let nombre = p.dataset.nombre.toLowerCase();
        p.style.display = nombre.includes(filtro) ? "block" : "none";
        p.parentNode.appendChild(p); // reordenar
    });
}

let esperandoVenta = false;
function iniciarCompra(){
    let productos = [];
    let total = 0;

    document.querySelectorAll(".producto").forEach(row=>{
        let check = row.querySelector(".check").checked;
        let cantidad = parseInt(row.querySelector(".cantidad").value) || 0;
        let nombre = row.dataset.nombre;
        let precio = parseFloat(row.dataset.precio);

        if(check && cantidad > 0){
            productos.push({nombre,cantidad,precio});
            total += precio * cantidad;
        }
    });

    if(productos.length === 0){
        alert("Selecciona productos con cantidad");
        return;
    }

    document.getElementById("estado_venta").innerText = "⏳ Pase la tarjeta...";

    esperandoVenta = true;

    let interval = setInterval(()=>{
        if(esperandoVenta){
            fetch("/leer_uid")
            .then(r=>r.json())
            .then(data=>{
                if(data.uid){
                    esperandoVenta = false;
                    clearInterval(interval);

                    fetch("/procesar_compra",{
                        method:"POST",
                        headers:{"Content-Type":"application/json"},
                        body: JSON.stringify({
                            uid: data.uid,
                            productos: productos   // 🔥 FIX
                        })
                    })
                    .then(r=>r.json())
                    .then(res=>{
                        alert(res.mensaje);
                        location.reload();
                    });
                }
            });
        }
    },1000);
}

let esperandoRecarga = false;
function iniciarRecarga(){
    fetch("/iniciar_recarga", {method:"POST"});
    document.getElementById("estado_recarga").innerText = "⏳ Pase la tarjeta...";
    esperandoRecarga = true;
}

setInterval(()=>{
    // esperandoVenta ya se maneja dentro de iniciarCompra() con su propio interval

    if(esperandoRecarga){
        fetch("/leer_uid").then(r=>r.json()).then(data=>{
            if(data.uid){
                esperandoRecarga=false;

                document.getElementById("estado_recarga").innerText = "UID: "+data.uid;
                document.getElementById("form_recarga").style.display="block";

                fetch("/info_tarjeta/"+data.uid)
                .then(r=>r.json())
                .then(info=>{
                    document.getElementById("nombre").value = info.nombre;
                    document.getElementById("saldo_actual").value = info.saldo;
                });
            }
        });
    }
},1000);

function guardarRecarga(){
    let nombre = document.getElementById("nombre").value;
    let monto = document.getElementById("monto").value;

    fetch("/guardar_recarga",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({nombre,monto})
    }).then(r=>r.json()).then(res=>{
        alert(res.mensaje);
        location.reload();
    });
}

let modo = "";

function modoUsuarios(m){
    modo = m;

    document.getElementById("panel_consulta").style.display = (m==="consulta")?"block":"none";
    document.getElementById("panel_form").style.display = "block"; // 🔥 SIEMPRE visible

    if(m==="registro"){
        document.querySelectorAll("#panel_form input").forEach(i=>i.value="");
        document.getElementById("resultado").innerHTML = "";
    }
}

function leerUID(){
    let interval = setInterval(()=>{
        fetch("/leer_uid")
        .then(r=>r.json())
        .then(data=>{
            if(data.uid){
                clearInterval(interval);

                // 👉 Siempre poner UID
                document.getElementById("uid").value = data.uid;

                // 👉 Siempre cargar usuario (para TODOS los modos)
                cargarUsuario(data.uid);
            }
        });
    },1000);
}

function cargarUsuario(uid){
    fetch("/consultar_usuario/"+uid)
    .then(r=>r.json())
    .then(u=>{

        // 👉 Llenar SIEMPRE inputs
        document.getElementById("uid").value = u.uid;
        document.getElementById("nombres").value = (u.nombres === "N/A") ? "" : u.nombres;
        document.getElementById("apellidos").value = (u.apellidos === "N/A") ? "" : u.apellidos;
        document.getElementById("correo").value = (u.correo === "N/A") ? "" : u.correo;
        document.getElementById("telefono").value = (u.telefono === "N/A") ? "" : u.telefono;
        document.getElementById("institucion").value = (u.institucion === "N/A") ? "" : u.institucion;
        document.getElementById("curso").value = (u.curso === "N/A") ? "" : u.curso;

        // 👉 Mostrar bonito en consulta
        document.getElementById("resultado").innerHTML = `
        <b>UID:</b> ${u.uid}<br>
        <b>Nombres:</b> ${u.nombres}<br>
        <b>Apellidos:</b> ${u.apellidos}<br>
        <b>Correo:</b> ${u.correo}<br>
        <b>Teléfono:</b> ${u.telefono}<br>
        <b>Institución:</b> ${u.institucion}<br>
        <b>Curso:</b> ${u.curso}
        `;
    });
}

function buscarNombre(){
    let nombre = document.getElementById("buscar_nombre").value;

    fetch("/buscar_usuario_nombre/"+nombre)
    .then(r=>r.json())
    .then(data=>{
        let html = "";

        data.forEach(u=>{
            html += `
            <hr>
            <b>UID:</b> ${u.uid}<br>
            <b>Nombre:</b> ${u.nombres} ${u.apellidos}<br>
            `;
        });

        document.getElementById("resultado").innerHTML = html || "Sin resultados";
    });
}

function guardarUsuario(){
    let data = {
        uid: document.getElementById("uid").value,
        nombres: document.getElementById("nombres").value,
        apellidos: document.getElementById("apellidos").value,
        correo: document.getElementById("correo").value,
        telefono: document.getElementById("telefono").value,
        institucion: document.getElementById("institucion").value,
        curso: document.getElementById("curso").value
    };

    fetch("/guardar_usuario",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(data)
    })
    .then(r=>r.json())
    .then(res=> alert(res.mensaje));
}

function mostrarPanel(p){
    document.getElementById("panel_modificar").style.display = (p==="modificar")?"block":"none";
    document.getElementById("panel_tabla").style.display = (p==="tabla")?"block":"none";
    document.getElementById("panel_credenciales").style.display = (p==="credenciales")?"block":"none";
    document.getElementById("panel_retiro").style.display = (p==="retiro")?"block":"none";
    document.getElementById("panel_bloqueo").style.display = (p==="bloqueo")?"block":"none";

    if(p==="tabla"){
        cargarTabla("fecha_desc");
    }
}

function cambiarCredenciales(){
    let uid = document.getElementById("cred_uid").value.trim();
    let user = document.getElementById("cred_user").value.trim();
    let pass = document.getElementById("cred_pass").value.trim();

    if(!uid || !user || !pass){
        document.getElementById("cred_resultado").innerHTML = "<span style='color:#f88;'>❌ Completa todos los campos</span>";
        return;
    }

    fetch("/cambiar_credenciales", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({uid, user, pass})
    })
    .then(r => r.json())
    .then(res => {
        document.getElementById("cred_resultado").innerHTML =
            `<span style='color:${res.ok ? "#6f6" : "#f88"};'>${res.mensaje}</span>`;
    })
    .catch(() => {
        document.getElementById("cred_resultado").innerHTML = "<span style='color:#f88;'>❌ Error de conexión</span>";
    });
}

function consultarAhorro(){
    let uid = document.getElementById("retiro_uid").value.trim();
    if(!uid){ alert("Ingresa un UID"); return; }

    fetch("/consultar_ahorro_admin/" + uid)
    .then(r => r.json())
    .then(data => {
        if(!data.ok){
            document.getElementById("retiro_info").innerText = "❌ " + data.mensaje;
            return;
        }
        document.getElementById("retiro_info").innerText =
            `👤 ${data.nombre} — Ahorro total: $${Number(data.ahorro).toFixed(2)}`;
    })
    .catch(() => {
        document.getElementById("retiro_info").innerText = "❌ Error de conexión";
    });
}

function procesarRetiro(){
    let uid = document.getElementById("retiro_uid").value.trim();
    let monto = parseFloat(document.getElementById("retiro_monto").value);

    if(!uid || isNaN(monto) || monto <= 0){
        document.getElementById("retiro_resultado").innerHTML = "<span style='color:#f88;'>❌ Datos inválidos</span>";
        return;
    }

    if(!confirm(`¿Confirmar retiro de $${monto.toFixed(2)} del ahorro del cliente?`)) return;

    fetch("/procesar_retiro", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({uid, monto})
    })
    .then(r => r.json())
    .then(res => {
        document.getElementById("retiro_resultado").innerHTML =
            `<span style='color:${res.ok ? "#6f6" : "#f88"};'>${res.mensaje}</span>`;
        if(res.ok) consultarAhorro(); // refrescar el saldo mostrado
    })
    .catch(() => {
        document.getElementById("retiro_resultado").innerHTML = "<span style='color:#f88;'>❌ Error de conexión</span>";
    });
}

function cargarTabla(orden){
    fetch("/listar_usuarios_con_estado?orden="+orden)
    .then(r=>r.json())
    .then(data=>{
        let tbody = document.querySelector("#tabla_usuarios tbody");
        tbody.innerHTML = "";

        data.forEach(u=>{
            let estadoBadge = u.bloqueada
                ? `<span style="background:#dc3545;color:white;padding:2px 8px;border-radius:10px;font-size:12px;">🔒 Bloqueada</span>`
                : `<span style="background:#198754;color:white;padding:2px 8px;border-radius:10px;font-size:12px;">✅ Activa</span>`;

            let btnBloqueo = u.bloqueada
                ? `<button onclick="bloquearDesdeTabla('${u.uid}', false)" style="padding:3px 8px;background:#198754;color:white;border:none;border-radius:5px;cursor:pointer;font-size:12px;">🔓 Desbloquear</button>`
                : `<button onclick="bloquearDesdeTabla('${u.uid}', true)" style="padding:3px 8px;background:#dc3545;color:white;border:none;border-radius:5px;cursor:pointer;font-size:12px;">🔒 Bloquear</button>`;

            let fila = `
            <tr id="fila_${u.uid}">
                <td>${u.uid}</td>
                <td>${u.apellidos} ${u.nombres}</td>
                <td>${u.correo}</td>
                <td>${u.telefono}</td>
                <td>${u.institucion}</td>
                <td>${u.curso}</td>
                <td>${u.fecha}</td>
                <td style="text-align:center;">${estadoBadge}</td>
                <td style="text-align:center;">${btnBloqueo}</td>
            </tr>
            `;
            tbody.innerHTML += fila;
        });
    });
}

function bloquearDesdeTabla(uid, bloquear){
    let confirmMsg = bloquear
        ? `¿Bloquear la tarjeta del UID ${uid}?`
        : `¿Desbloquear la tarjeta del UID ${uid}?`;

    if(!confirm(confirmMsg)) return;

    let url = bloquear ? "/bloquear_tarjeta" : "/desbloquear_tarjeta";
    let body = bloquear ? {uid: uid, motivo: "Bloqueada desde tabla de usuarios"} : {uid: uid};

    fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)
    })
    .then(r => r.json())
    .then(res => {
        alert(res.mensaje);
        if(res.ok) cargarTabla(document.querySelector("#panel_tabla select").value || "fecha_desc");
    })
    .catch(() => alert("❌ Error de conexión"));
}

function consultarEstadoTarjeta(){
    let uid = document.getElementById("bloqueo_uid").value.trim();
    if(!uid){ alert("Ingresa un UID"); return; }

    fetch("/estado_tarjeta/" + uid)
    .then(r => r.json())
    .then(data => {
        let infoDiv = document.getElementById("bloqueo_info");
        let botonesDiv = document.getElementById("bloqueo_botones");
        let motivoDiv = document.getElementById("bloqueo_motivo_div");

        if(!data.ok){
            infoDiv.style.display = "block";
            infoDiv.style.background = "#4a1a1a";
            infoDiv.innerHTML = `❌ ${data.mensaje}`;
            botonesDiv.style.display = "none";
            motivoDiv.style.display = "none";
            return;
        }

        infoDiv.style.display = "block";
        botonesDiv.style.display = "flex";

        if(data.bloqueada){
            infoDiv.style.background = "#4a1a1a";
            infoDiv.innerHTML = `<strong>👤 ${data.nombre}</strong><br>Estado: <span style="color:#f88;font-weight:bold;">🔒 BLOQUEADA</span>`;
            document.getElementById("btn_bloquear").style.display = "none";
            document.getElementById("btn_desbloquear").style.display = "inline-block";
            motivoDiv.style.display = "none";
        } else {
            infoDiv.style.background = "#1a3a1a";
            infoDiv.innerHTML = `<strong>👤 ${data.nombre}</strong><br>Estado: <span style="color:#6f6;font-weight:bold;">✅ ACTIVA</span>`;
            document.getElementById("btn_bloquear").style.display = "inline-block";
            document.getElementById("btn_desbloquear").style.display = "none";
            motivoDiv.style.display = "block";
        }

        document.getElementById("bloqueo_resultado").innerHTML = "";
    })
    .catch(() => {
        document.getElementById("bloqueo_info").style.display = "block";
        document.getElementById("bloqueo_info").innerHTML = "❌ Error de conexión";
    });
}

function ejecutarBloqueo(){
    let uid = document.getElementById("bloqueo_uid").value.trim();
    let motivo = document.getElementById("bloqueo_motivo").value;

    if(!confirm(`¿Confirmar bloqueo de tarjeta ${uid} por: ${motivo}?`)) return;

    fetch("/bloquear_tarjeta", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({uid: uid, motivo: motivo})
    })
    .then(r => r.json())
    .then(res => {
        document.getElementById("bloqueo_resultado").innerHTML =
            `<span style='color:${res.ok ? "#6f6" : "#f88"};'>${res.mensaje}</span>`;
        if(res.ok) consultarEstadoTarjeta();
    })
    .catch(() => {
        document.getElementById("bloqueo_resultado").innerHTML = "<span style='color:#f88;'>❌ Error de conexión</span>";
    });
}

function ejecutarDesbloqueo(){
    let uid = document.getElementById("bloqueo_uid").value.trim();

    if(!confirm(`¿Confirmar desbloqueo de tarjeta ${uid}?`)) return;

    fetch("/desbloquear_tarjeta", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({uid: uid})
    })
    .then(r => r.json())
    .then(res => {
        document.getElementById("bloqueo_resultado").innerHTML =
            `<span style='color:${res.ok ? "#6f6" : "#f88"};'>${res.mensaje}</span>`;
        if(res.ok) consultarEstadoTarjeta();
    })
    .catch(() => {
        document.getElementById("bloqueo_resultado").innerHTML = "<span style='color:#f88;'>❌ Error de conexión</span>";
    });
}
</script>
</head>

<body>

<script>
function cerrarSemana(){
    fetch("/cerrar_semana")
    .then(r=>r.text())
    .then(msg=>{
        alert(msg);
    });
}
</script>

<!-- HEADER -->
<div class="header">
    <div class="logo">
        <img src="/static/logo.png" width="40">
        BarPay
    </div>
    <button onclick="location.href='/logout'">Cerrar sesión</button>
</div>

<!-- SIDEBAR -->
<div class="sidebar">
    <button onclick="mostrarSeccion('inicio')">🏠 Inicio</button>
    <button onclick="mostrarSeccion('venta')">🛒 Venta</button>
    <button onclick="mostrarSeccion('recarga')">💰 Recargas</button>
    <button onclick="mostrarSeccion('historial')">📋 Historial</button>
    <button onclick="mostrarSeccion('usuarios')">👤 Usuarios</button>
    <button onclick="mostrarSeccion('ranking')">🏆 Ranking</button>
    <button onclick="cerrarSemana()">📅 Cerrar semana</button>
</div>

<!-- MAIN -->
<div class="main">

    <!-- INICIO -->
    <div id="inicio" class="seccion"></div>

    <!-- VENTA -->
    <div id="venta" class="seccion" style="display:none;">
        <h2>Venta</h2>

        <input type="text" id="buscador" placeholder="Buscar..." onkeyup="filtrarProductos()">

        <div>
        {% for p in productos %}
        <div class="producto" data-nombre="{{p.nombre}}" data-precio="{{p.precio}}">
            <input type="checkbox" class="check">
            {{p.nombre}} (${{p.precio}})
            <input type="number" class="cantidad" value="1">
        </div>
        {% endfor %}
        </div>

        <br><br>
        <button onclick="iniciarCompra()">Comprar</button>

        <h3 id="estado_venta"></h3>
    </div>

    <!-- RECARGA -->
    <div id="recarga" class="seccion" style="display:none;">
        <h2>Recargas</h2>

        <button onclick="iniciarRecarga()">Esperar tarjeta</button>
        <h3 id="estado_recarga"></h3>

        <div id="form_recarga" style="display:none;">
            Nombre: <input id="nombre"><br>
            Saldo: <input id="saldo_actual" readonly><br>
            Monto: <input id="monto"><br>
            <button onclick="guardarRecarga()">Guardar</button>
        </div>
    </div>

    <!-- HISTORIAL -->
    <div id="historial" class="seccion" style="display:none;">
        <h2>📜 Historial</h2>

        <!-- 🔍 PANEL DE BÚSQUEDA (ARRIBA) -->
        <div style="
            background:#1e293b;
            color: white;
            padding:15px;
            border-radius:10px;
            margin-bottom:20px;
        ">

            <h3 style="margin-top:0;">🔍 Buscar transacción</h3>

            <input id="buscar_id" type="number" placeholder="Ej: 15" style="padding:5px;">
            <button id="btnBuscar">Buscar</button>

            <div id="preview_mov" style="margin-top:10px;"></div>

            <br>

            <button id="btnRevertir">❌ Revertir</button>

        </div>

        <!-- 📊 TABLA -->
        <table border="1" width="100%">
        <tr>
            <th>ID</th>
            <th>UID</th>
            <th>Nombre</th>
            <th>Pago</th>
            <th>Detalle</th>
            <th>Movimiento</th>
            <th>Fecha</th>
        </tr>

        {% for f in historial %}
        <tr>
            <td>{{f[0]}}</td>
            <td>{{f[1]}}</td>
            <td>{{f[2] or 'Sin nombre'}}</td>

            <td>
            {% if f[5] == 'compra' %}
                -${{ "%.2f"|format(f[3]) }}
            {% elif f[5] == 'recarga' %}
                +${{ "%.2f"|format(f[3]) }}
            {% elif f[5] == 'reverso' %}
                🔁 ${{ "%.2f"|format(f[3]) }}
            {% else %}
                ${{ "%.2f"|format(f[3]) }}
            {% endif %}
            </td>

            <td>{{f[4] or '-'}}</td>
            <td>{{f[5]}}</td>
            <td>{{f[6]}}</td>
        </tr>
        {% endfor %}
        </table>
    </div>

<!-- USUARIOS -->
<div id="usuarios" class="seccion" style="display:none;">
    <h2>Usuarios</h2>

    <button onclick="mostrarPanel('modificar')">✏️ Registrar/Modificar</button>
    <button onclick="mostrarPanel('tabla')">📋 Usuarios Registrados</button>
    <button onclick="mostrarPanel('credenciales')">🔑 Cambiar Credenciales</button>
    <button onclick="mostrarPanel('retiro')">💸 Retiro de Ahorro</button>
    <button onclick="mostrarPanel('bloqueo')">🔒 Bloquear Tarjeta</button>

    <!-- MODIFICAR -->
    <div id="panel_modificar" style="display:none;">
        <h3>Registrar/Modificar Usuario</h3>

        <button onclick="leerUID()">📡 Leer tarjeta</button><br><br>

        UID: <input id="uid"><br>
        Nombres: <input id="nombres"><br>
        Apellidos: <input id="apellidos"><br>
        Correo: <input id="correo"><br>
        Teléfono: <input id="telefono"><br>
        Institución: <input id="institucion"><br>
        Curso: <input id="curso"><br><br>

        <button onclick="guardarUsuario()">Guardar</button>

        <h3>Información</h3>
        <div id="resultado"></div>
    </div>

    <!-- TABLA -->
    <div id="panel_tabla" style="display:none;">
        <h3>Usuarios Registrados</h3>

        Ordenar por:
        <select onchange="cargarTabla(this.value)">
            <option value="fecha_desc">Fecha ↓</option>
            <option value="fecha_asc">Fecha ↑</option>
            <option value="uid">UID</option>
            <option value="nombre">Nombre</option>
        </select>

        <br><br>

        <table border="1" id="tabla_usuarios">
            <thead>
                <tr>
                    <th>UID</th>
                    <th>Nombre</th>
                    <th>Correo</th>
                    <th>Teléfono</th>
                    <th>Institución</th>
                    <th>Curso</th>
                    <th>Fecha</th>
                    <th>Estado</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>
    </div>

    <!-- CAMBIO DE CREDENCIALES -->
    <div id="panel_credenciales" style="display:none; margin-top:20px; padding:15px; background:#1e293b; color:white; border-radius:10px;">
        <h3>🔑 Cambio de usuario y contraseña del cliente</h3>
        <p style="color:#aaa;">Busca al usuario por UID o nombre, luego actualiza sus credenciales de acceso.</p>

        UID del cliente: <input id="cred_uid" style="padding:6px; border-radius:5px; border:none;"><br><br>
        Nuevo usuario (para login): <input id="cred_user" style="padding:6px; border-radius:5px; border:none;"><br><br>
        Nueva contraseña: <input id="cred_pass" type="password" style="padding:6px; border-radius:5px; border:none;"><br><br>
        <button onclick="cambiarCredenciales()" style="padding:8px 16px; background:#0d6efd; color:white; border:none; border-radius:5px; cursor:pointer;">💾 Guardar credenciales</button>
        <div id="cred_resultado" style="margin-top:10px;"></div>
    </div>

    <!-- RETIRO DE AHORRO -->
    <div id="panel_retiro" style="display:none; margin-top:20px; padding:15px; background:#1e293b; color:white; border-radius:10px;">
        <h3>💸 Retiro de ahorro del cliente</h3>
        <p style="color:#aaa;">Ingresa el UID del cliente y el monto a retirar de su ahorro acumulado.</p>

        UID del cliente: <input id="retiro_uid" style="padding:6px; border-radius:5px; border:none;"><br><br>
        <button onclick="consultarAhorro()" style="padding:6px 12px; background:#444; color:white; border:none; border-radius:5px; cursor:pointer;">🔍 Consultar ahorro</button>
        <div id="retiro_info" style="margin:10px 0; color:#aaa;"></div>

        Monto a retirar: <input id="retiro_monto" type="number" step="0.01" min="0.01" style="padding:6px; border-radius:5px; border:none;"><br><br>
        <button onclick="procesarRetiro()" style="padding:8px 16px; background:#dc3545; color:white; border:none; border-radius:5px; cursor:pointer;">💸 Procesar retiro</button>
        <div id="retiro_resultado" style="margin-top:10px;"></div>
    </div>

    <!-- BLOQUEO DE TARJETA -->
    <div id="panel_bloqueo" style="display:none; margin-top:20px; padding:15px; background:#1e293b; color:white; border-radius:10px;">
        <h3>🔒 Bloqueo de tarjeta por robo o pérdida</h3>
        <p style="color:#aaa;">Busca la tarjeta por UID y bloquéala o desbloquéala. Una tarjeta bloqueada no podrá realizar compras ni recargas.</p>

        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <input id="bloqueo_uid" placeholder="UID de la tarjeta" style="padding:8px; border-radius:5px; border:none; flex:1; min-width:180px;">
            <button onclick="consultarEstadoTarjeta()" style="padding:8px 14px; background:#444; color:white; border:none; border-radius:5px; cursor:pointer;">🔍 Consultar</button>
        </div>

        <div id="bloqueo_info" style="margin:12px 0; padding:10px; border-radius:8px; display:none;"></div>

        <div id="bloqueo_motivo_div" style="display:none; margin-bottom:10px;">
            Motivo del bloqueo:<br>
            <select id="bloqueo_motivo" style="padding:8px; border-radius:5px; border:none; margin-top:5px; width:100%;">
                <option value="Robo">🚨 Robo</option>
                <option value="Pérdida">📭 Pérdida</option>
                <option value="Solicitud del titular">👤 Solicitud del titular</option>
                <option value="Uso sospechoso">⚠️ Uso sospechoso</option>
            </select>
        </div>

        <div id="bloqueo_botones" style="display:none; gap:10px; flex-wrap:wrap;">
            <button id="btn_bloquear" onclick="ejecutarBloqueo()" style="padding:8px 16px; background:#dc3545; color:white; border:none; border-radius:5px; cursor:pointer;">🔒 Bloquear tarjeta</button>
            <button id="btn_desbloquear" onclick="ejecutarDesbloqueo()" style="padding:8px 16px; background:#198754; color:white; border:none; border-radius:5px; cursor:pointer;">🔓 Desbloquear tarjeta</button>
        </div>

        <div id="bloqueo_resultado" style="margin-top:12px;"></div>
    </div>
</div>

<!-- RANKING -->
<div id="ranking" class="seccion" style="display:none;">
    <h2>🏆 Top Ahorradores</h2>

    <table border="1">
        <tr>
            <th>#</th>
            <th>Nombre</th>
            <th>Puntos</th>
        </tr>
        <tbody id="tablaRanking">
            <tr><td colspan="3">Cargando...</td></tr>
        </tbody>
    </table>
</div>
```

</div>


</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<script>
// =============================
// 🔥 VARIABLES GLOBALES
// =============================
let movimientoActual = null;
let uid_actual = "";

// =============================
// 📊 GRÁFICAS
// =============================
document.addEventListener("DOMContentLoaded", function(){

    fetch("/datos_ahorro")
    .then(r => r.json())
    .then(data => {

        let canvasAhorro = document.getElementById("graficaAhorro");
        let canvasGastos = document.getElementById("graficaGastos");
        let canvasProy = document.getElementById("graficaProyeccion");

        if (!canvasAhorro) return;

        let semanas = data.ahorro.map(x => x[0]);
        let ahorro = data.ahorro.map(x => x[1]);

        if (ahorro.length === 0) {
            canvasAhorro.innerHTML = "Sin datos";
            return;
        }

        new Chart(canvasAhorro, {
            type: "line",
            data: {
                labels: semanas,
                datasets: [{
                    label: "Ahorro",
                    data: ahorro
                }]
            }
        });

        if (canvasGastos) {
            let gastos = data.gastos.map(x => x[1]);

            new Chart(canvasGastos, {
                type: "bar",
                data: {
                    labels: semanas,
                    datasets: [{
                        label: "Gastos",
                        data: gastos
                    }]
                }
            });
        }

        if (ahorro.length < 2 || !canvasProy) return;

        let tendencia = ahorro[ahorro.length - 1] - ahorro[0];
        let proyeccion = [...ahorro];

        for (let i = 1; i <= 5; i++) {
            proyeccion.push(ahorro[ahorro.length - 1] + (tendencia / 5) * i);
        }

        new Chart(canvasProy, {
            type: "line",
            data: {
                labels: [...semanas, "+1", "+2", "+3", "+4", "+5"],
                datasets: [{
                    label: "Proyección",
                    data: proyeccion
                }]
            }
        });

    })
    .catch(err => console.error("Error en gráficas:", err));

});


// =============================
// 💰 SLIDER DE AHORRO
// =============================
document.addEventListener("DOMContentLoaded", function(){

    let saldo = {{ saldo | default(0) }};
    let slider = document.getElementById("sliderAhorro");

    if(!slider) return;

    function actualizarBloqueo(){
        let porcentaje = slider.value;

        let bloqueado = saldo * porcentaje / 100;
        let disponible = saldo - bloqueado;

        document.getElementById("saldoBloqueado").innerText = "$ " + bloqueado.toFixed(2);
        document.getElementById("saldoDisponible").innerText = "$ " + disponible.toFixed(2);
    }

    slider.oninput = actualizarBloqueo;
    actualizarBloqueo();
});


// =============================
// 🏆 RANKING
// =============================
function cargarRanking() {

    fetch("/ranking")
    .then(res => res.json())
    .then(data => {

        let tabla = document.getElementById("tablaRanking");
        if(!tabla) return;

        // compatibilidad con nuevo formato {top5, yo}
        let lista = data.top5 || data;

        let html = "";

        if(lista.length === 0){
            tabla.innerHTML = "<tr><td colspan='3'>Sin datos</td></tr>";
            return;
        }

        lista.forEach((r, i) => {

            let medal = "";
            if (i === 0) medal = "🥇";
            else if (i === 1) medal = "🥈";
            else if (i === 2) medal = "🥉";

            html += `
                <tr>
                    <td>${medal} ${r.posicion}</td>
                    <td>${r.nombre}</td>
                    <td>${Number(r.puntos).toFixed(2)}</td>
                </tr>
            `;
        });

        tabla.innerHTML = html;
    })
    .catch(err => {
        console.error(err);
        alert("❌ Error cargando ranking");
    });
}


// =============================
// 📜 HISTORIAL CLIENTE
// =============================
async function cargarHistorial(uid) {

    const res = await fetch(`/historial/${uid}`);
    const data = await res.json();

    const tabla = document.getElementById("historial-body");
    if (!tabla) return;

    tabla.innerHTML = "";

    data.forEach(item => {
        tabla.innerHTML += `
            <tr>
                <td>${item.fecha}</td>
                <td>${item.movimiento}</td>
                <td>$${item.pago}</td>
            </tr>
        `;
    });
}


// =============================
// 🔍 BUSCAR MOVIMIENTO
// =============================
function buscarMovimiento(){

    let id = document.getElementById("buscar_id").value;

    if(!id){
        alert("Ingresa un ID");
        return;
    }

    fetch("/buscar_movimiento/" + id)
    .then(r=>r.json())
    .then(data=>{

        if(!data.ok){
            document.getElementById("preview_mov").innerHTML = "❌ No encontrado";
            movimientoActual = null;
            return;
        }

        movimientoActual = data;

        document.getElementById("preview_mov").innerHTML = `
            <b>ID:</b> ${data.id}<br>
            <b>Usuario:</b> ${data.nombre} (${data.uid})<br>
            <b>Tipo:</b> ${data.movimiento}<br>
            <b>Monto:</b> $${data.pago.toFixed(2)}<br>
            <b>Detalle:</b> ${data.detalle || '-'}<br>
            <b>Fecha:</b> ${data.fecha}
        `;
    })
    .catch(err=>{
        console.error(err);
        alert("❌ Error al buscar");
    });
}


// =============================
// 🔁 REVERTIR
// =============================
function revertir(){

    if(!movimientoActual){
        alert("Busca primero una transacción válida");
        return;
    }

    if(!confirm(`¿Revertir ID #${movimientoActual.id}?`)){
        return;
    }

    if(!confirm("⚠️ Esta acción es irreversible. Confirmar nuevamente.")){
        return;
    }

    fetch("/revertir",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({id: movimientoActual.id})
    })
    .then(r=>r.json())
    .then(res=>{
        alert(res.mensaje);
        location.reload();
    })
    .catch(err=>{
        console.error(err);
        alert("❌ Error al revertir");
    });
}


// =============================
// ⚡ ENTER PARA BUSCAR
// =============================
document.addEventListener("DOMContentLoaded", function(){

    let btnBuscar = document.getElementById("btnBuscar");
    if(btnBuscar){
        btnBuscar.addEventListener("click", buscarMovimiento);
    }

    let btnRevertir = document.getElementById("btnRevertir");
    if(btnRevertir){
        btnRevertir.addEventListener("click", revertir);
    }

    let input = document.getElementById("buscar_id");
    if(input){
        input.addEventListener("keypress", function(e){
            if(e.key === "Enter"){
                buscarMovimiento();
            }
        });
    }

    // 🏠 Mostrar inicio por defecto al cargar/recargar
    mostrarSeccion('inicio');

});

</script>

</body>
</html>
"""

# ==============================
# login
# ==============================
@app.route("/login", methods=["GET","POST"])
def login():
    conn, cursor = get_db()

    if request.method == "POST":
        u = request.form.get("user")
        p = request.form.get("pass")
        tipo = request.form.get("tipo")

        # 🔴 VALIDACIÓN GLOBAL
        if not tipo:
            return render_template_string(LOGIN_HTML, error="⚠️ Selecciona tipo de acceso")

        if tipo == "admin":
            if u in USUARIOS and USUARIOS[u] == p:
                session.clear()
                session["login"] = True
                session["tipo"] = "admin"
                return redirect("/")
            else:
                return render_template_string(LOGIN_HTML, error="❌ Admin incorrecto")

        elif tipo == "cliente":
            if not u:
                return render_template_string(LOGIN_HTML, error="❌ Ingresa usuario o UID")

            uid_encontrado = None

            # 🔑 Intentar login con credenciales personalizadas
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS credenciales_cliente (
                    uid TEXT PRIMARY KEY,
                    usuario TEXT,
                    contrasena TEXT
                )
            """)
            cursor.execute(
                "SELECT uid FROM credenciales_cliente WHERE usuario=? AND contrasena=?",
                (u, p)
            )
            cred = cursor.fetchone()

            if cred:
                uid_encontrado = cred[0]
            else:
                # 🔄 Fallback: login por UID directo
                cursor.execute("SELECT uid FROM usuarios WHERE uid=?", (u,))
                row = cursor.fetchone()
                if row:
                    uid_encontrado = row[0]

            if uid_encontrado:
                # 🔒 Verificar si la tarjeta está bloqueada
                cursor.execute("SELECT bloqueada FROM tarjetas WHERE uid=?", (uid_encontrado,))
                tar = cursor.fetchone()
                if tar and tar[0]:
                    return render_template_string(LOGIN_HTML, error="🔒 Tarjeta bloqueada. Contacte al administrador.")

                session.clear()
                session["login"] = True
                session["tipo"] = "cliente"
                session["uid"] = uid_encontrado
                return redirect("/cliente")
            else:
                return render_template_string(LOGIN_HTML, error="❌ Usuario o contraseña incorrectos")

    return render_template_string(LOGIN_HTML, error="🔒 Tu tarjeta ha sido bloqueada. Contacte al administrador." if request.args.get("bloqueada") else "")

# ==============================
# HOME (ADMIN)
# ==============================
@app.route("/")
def index():
    conn, cursor = get_db()
    if not session.get("login"):
        return redirect("/login")

    if session.get("tipo") == "cliente":
        return redirect("/cliente")

    cursor.execute("""
        SELECT h.id,
               h.uid,
               t.nombre,
               h.pago,
               h.detalle,
               h.movimiento,
               h.fecha
        FROM historial h
        LEFT JOIN tarjetas t ON h.uid = t.uid
        ORDER BY h.id DESC
        LIMIT 50
    """)

    datos = cursor.fetchall()

    return render_template_string(
        HTML,
        historial=datos,
        productos=productos
    )


# ==============================
# CLIENTE
# ==============================
@app.route("/cliente")
def cliente():
    conn, cursor = get_db()
    if not session.get("login") or session.get("tipo") != "cliente":
        return redirect("/login")

    uid = session.get("uid")

    # Si las credenciales fueron cambiadas, forzar re-login
    if uid in _sesiones_invalidadas:
        _sesiones_invalidadas.discard(uid)
        session.clear()
        return redirect("/login")

    # 🔒 Verificar si la tarjeta fue bloqueada durante la sesión activa
    cursor.execute("SELECT bloqueada FROM tarjetas WHERE uid=?", (uid,))
    tar = cursor.fetchone()
    if tar and tar[0]:
        session.clear()
        return redirect("/login?bloqueada=1")

    # ------------------------------
    # SALDO
    # ------------------------------
    cursor.execute("SELECT saldo FROM tarjetas WHERE uid=?", (uid,))
    r = cursor.fetchone()
    saldo = float(r[0]) if r and r[0] else 0

    # ------------------------------
    # NOMBRE
    # ------------------------------
    cursor.execute("SELECT nombres, apellidos FROM usuarios WHERE uid=?", (uid,))
    u = cursor.fetchone()
    nombre = f"{u[0]} {u[1]}" if u else "Usuario"

    # ------------------------------
    # HISTORIAL
    # ------------------------------
    cursor.execute("""
        SELECT id, uid, movimiento, pago, fecha, detalle
        FROM historial
        WHERE uid=?
        ORDER BY id DESC
    """, (uid,))
    historial = cursor.fetchall()

    # ------------------------------
    # AHORRO TOTAL (incluye TODO)
    # ------------------------------
    cursor.execute("""
        SELECT COALESCE(SUM(monto), 0)
        FROM ahorros
        WHERE uid=?
    """, (uid,))
    ahorro_total = float(cursor.fetchone()[0])

    # ------------------------------
    # CONFIGURACIÓN
    # ------------------------------
    cursor.execute("""
        SELECT modo, valor
        FROM configuracion_ahorro
        WHERE uid=?
    """, (uid,))
    conf = cursor.fetchone()

    modo_conf = conf[0] if conf else "porcentaje"
    valor_conf = float(conf[1]) if conf else 0

    porcentaje_conf = valor_conf if modo_conf == "porcentaje" else 0
    monto_conf = valor_conf if modo_conf == "monto" else 0

    # ------------------------------
    # PUNTOS TOTALES
    # ------------------------------
    cursor.execute("SELECT puntos FROM puntos WHERE uid=?", (uid,))
    res = cursor.fetchone()
    puntos_totales = float(res[0]) if res and res[0] else 0

    # ------------------------------
    # 🧠 ÚLTIMA SEMANA REAL (CLAVE)
    # ------------------------------
    cursor.execute("""
        SELECT semana
        FROM ahorros
        WHERE uid=? AND tipo='interes'
        ORDER BY id DESC
        LIMIT 1
    """, (uid,))
    row = cursor.fetchone()

    if row:
        ultima_semana = row[0]

        cursor.execute("""
            SELECT COALESCE(SUM(monto), 0)
            FROM ahorros
            WHERE uid=? AND tipo='interes' AND semana=?
        """, (uid, ultima_semana))

        interes_semana = float(cursor.fetchone()[0])
    else:
        interes_semana = 0

    puntos_generados = round(interes_semana * 100, 2)

    # ------------------------------
    # 🏆 POSICIÓN EN RANKING
    # ------------------------------
    cursor.execute("""
        SELECT u.uid,
               COALESCE(p.puntos, 0) as puntos
        FROM usuarios u
        LEFT JOIN puntos p ON u.uid = p.uid
        ORDER BY puntos DESC
    """)

    ranking = cursor.fetchall()

    posicion = 0
    total = len(ranking)

    for i, row in enumerate(ranking):
        if row[0] == uid:
            posicion = i + 1
            break

    total_usuarios = len(ranking)

    # ------------------------------
    # META
    # ------------------------------
    cursor.execute("""
        SELECT meta
        FROM metas_ahorro
        WHERE uid=?
    """, (uid,))
    meta_row = cursor.fetchone()
    meta_ahorro = float(meta_row[0]) if meta_row and meta_row[0] else 50

    return render_template_string(
        CLIENTE_HTML,
        saldo=saldo,
        ahorro_total=ahorro_total,
        historial=historial,
        puntos_totales=round(puntos_totales, 2),
        puntos_generados=round(puntos_generados, 2),
        nombre=nombre,
        modo_conf=modo_conf,
        valor_conf=valor_conf,
        porcentaje_conf=porcentaje_conf,
        monto_conf=monto_conf,
        meta_ahorro=meta_ahorro,
        uid=uid,
        posicion=posicion,
        total_usuarios=total
    )

# ==============================
# LOGOUT
# ==============================
@app.route("/logout")
def logout():
    try:
        session.clear()
    except Exception:
        pass
    response = redirect("/login")
    # Limpiar cookie de sesion por si acaso
    response.delete_cookie("session")
    return response


# ==============================
# DATOS PARA GRÁFICAS
# ==============================
@app.route("/datos_ahorro")
def datos_ahorro():
    conn, cursor = get_db()

    if not requiere_login("cliente"):
        return jsonify({"ahorro": [], "gastos": []})

    uid = session.get("uid")

    if not uid:
        return jsonify({"ahorro": [], "gastos": []})

    # ------------------------------
    # AHORRO (últimas 5 semanas)
    # ------------------------------
    cursor.execute("""
        SELECT semana, monto
        FROM ahorros
        WHERE uid=? AND tipo='ahorro'
        ORDER BY semana DESC
        LIMIT 5
    """, (uid,))
    ahorro = cursor.fetchall()[::-1]

    # ------------------------------
    # GASTOS
    # ------------------------------
    cursor.execute("""
        SELECT fecha, pago
        FROM historial
        WHERE uid=? AND movimiento='compra'
        ORDER BY fecha DESC
        LIMIT 5
    """, (uid,))

    gastos_raw = cursor.fetchall()[::-1]

    gastos = []
    for fecha, monto in gastos_raw:
        gastos.append((fecha, float(monto)))

    return jsonify({
        "ahorro": ahorro,
        "gastos": gastos
    })


# ==============================
# CERRAR SEMANA
# ==============================
@app.route("/cerrar_semana")
def cerrar_semana():
    conn, cursor = get_db()

    if not requiere_login("admin"):
        return "❌ No autorizado", 403

    now = datetime.now()

    if MODO_TEST:
        semana = f"{now.year}-{now.isocalendar()[1]}-{int(time.time())}"
    else:
        semana = f"{now.year}-{now.isocalendar()[1]}"

    cursor.execute("SELECT uid, saldo FROM tarjetas")
    usuarios = cursor.fetchall()

    for uid, saldo in usuarios:

        # 🔴 EVITAR DUPLICADOS
        cursor.execute("""
            SELECT 1 FROM ahorros
            WHERE uid=? AND semana=? AND tipo='ahorro'
        """, (uid, semana))

        if cursor.fetchone():
            continue

        # ------------------------------
        # CONFIGURACIÓN
        # ------------------------------
        cursor.execute("""
            SELECT modo, valor
            FROM configuracion_ahorro
            WHERE uid=?
        """, (uid,))
        conf = cursor.fetchone()

        if not conf:
            continue

        modo, valor = conf

        # ------------------------------
        # TOTAL ANTES DEL AHORRO 🔥
        # ------------------------------
        cursor.execute("""
            SELECT COALESCE(SUM(monto), 0)
            FROM ahorros
            WHERE uid=?
        """, (uid,))
        ahorro_total_anterior = float(cursor.fetchone()[0])

        # ------------------------------
        # CÁLCULO AHORRO
        # ------------------------------
        if modo == "porcentaje":
            ahorro = saldo * (valor / 100)
        else:
            ahorro = valor

        if ahorro <= 0:
            continue

        if ahorro > saldo:
            ahorro = saldo

        # ------------------------------
        # GUARDAR AHORRO
        # ------------------------------
        cursor.execute("""
            INSERT INTO ahorros (uid, semana, monto, tipo)
            VALUES (?, ?, ?, 'ahorro')
        """, (uid, semana, ahorro))

        cursor.execute("""
            INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
            VALUES (?, ?, 'ahorro', datetime('now'), 'Ahorro semanal')
        """, (uid, ahorro))

        # ------------------------------
        # INTERÉS (SOBRE TOTAL ANTERIOR) ✅
        # ------------------------------
        if ahorro_total_anterior >= 50:
            interes_semanal = ahorro_total_anterior * INTERES_SEMANAL  # sin redondeo interno
        else:
            interes_semanal = 0

        puntos_generados = interes_semanal * 100  # sin redondeo interno

        # ------------------------------
        # GUARDAR INTERÉS
        # ------------------------------
        if interes_semanal > 0:

            cursor.execute("""
                INSERT INTO ahorros (uid, semana, monto, tipo)
                VALUES (?, ?, ?, 'interes')
            """, (uid, semana, interes_semanal))

            cursor.execute("""
                INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
                VALUES (?, ?, 'interes', datetime('now'), 'Interés semanal')
            """, (uid, interes_semanal))

        # ------------------------------
        # GUARDAR PUNTOS
        # ------------------------------
        if puntos_generados > 0:
            cursor.execute("""
                INSERT INTO puntos (uid, puntos)
                VALUES (?, ?)
                ON CONFLICT(uid)
                DO UPDATE SET puntos = puntos + ?
            """, (uid, puntos_generados, puntos_generados))

        # ------------------------------
        # DESCONTAR SALDO
        # ------------------------------
        cursor.execute("""
            UPDATE tarjetas
            SET saldo = saldo - ?
            WHERE uid=?
        """, (ahorro, uid))

    conn.commit()

    return "✅ Semana cerrada correctamente"

# ==============================
# INFO TARJETA
# ==============================
@app.route("/info_tarjeta/<uid>")
def info_tarjeta(uid):
    conn, cursor = get_db()

    cursor.execute("SELECT nombre, saldo FROM tarjetas WHERE uid=?", (uid,))
    r = cursor.fetchone()

    if r:
        return jsonify({
            "nombre": r[0],
            "saldo": r[1]
        })

    cursor.execute("SELECT nombres, apellidos FROM usuarios WHERE uid=?", (uid,))
    u = cursor.fetchone()

    if u:
        return jsonify({
            "nombre": f"{u[0]} {u[1]}",
            "saldo": 0
        })

    return jsonify({
        "nombre": "",
        "saldo": 0
    })

# ==============================
# uid
# ==============================
@app.route("/leer_uid")
def leer_uid():
    global uid_recarga

    if not cola_uid.empty():
        uid = cola_uid.get()

        uid_recarga = uid  # 🔥 ESTA LÍNEA ES LA CLAVE

        return jsonify({
            "uid": uid
        })

    return jsonify({
        "uid": ""
    })

# ==============================
# compra
# ==============================
@app.route("/iniciar_compra", methods=["POST"])
def iniciar_compra():
    global venta_data
    venta_data = request.json
    return jsonify({"ok": True})

@app.route("/procesar_compra", methods=["POST"])
def procesar_compra():
    conn, cursor = get_db()
    global venta_data

    data = request.json
    productos = data.get("productos", [])
    uid = data.get("uid")

    # ❌ No hay productos
    if not productos:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ No hay productos"})

    # ❌ No hay UID
    if not uid:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ No se detectó tarjeta"})

    # 💰 Calcular total
    total = 0
    for p in productos:
        try:
            total += float(p["precio"]) * int(p["cantidad"])
        except:
            return jsonify({"mensaje": "❌ Datos inválidos"})

    # 🔥 Guardar venta (ESTO ES LO QUE TE FALTABA)
    venta_data = {
        "uid": uid,
        "productos": productos,
        "total": total
    }

    # 🔍 Buscar usuario en tarjetas
    cursor.execute("SELECT nombre, saldo, bloqueada FROM tarjetas WHERE uid=?", (uid,))
    r = cursor.fetchone()

    # ❌ Usuario no existe
    if not r:
        venta_data = {}
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ Usuario no encontrado"})

    nombre = r[0]
    saldo = r[1]
    bloqueada = r[2]

    # 🔒 Tarjeta bloqueada
    if bloqueada:
        venta_data = {}
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "🔒 Tarjeta bloqueada por robo/pérdida. Contacte al administrador."})

    # ❌ Saldo insuficiente
    if saldo < total:
        venta_data = {}
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({
            "mensaje": f"❌ Saldo insuficiente (${round(saldo,2)})"
        })

    # ✅ Calcular nuevo saldo
    nuevo = saldo - total

    # 💾 Actualizar saldo
    cursor.execute(
        "UPDATE tarjetas SET saldo=? WHERE uid=?",
        (nuevo, uid)
    )

    # 🧾 Guardar historial (COMPATIBLE CON TU TABLA)
    productos_str = ""

    for p in productos:
        p_nombre = p.get("nombre", "")
        cantidad = int(p.get("cantidad", 0))

        if cantidad > 0:
            if productos_str != "":
                productos_str += ", "
            productos_str += f"{p_nombre} x{cantidad}"

    cursor.execute("""
        INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
        VALUES (?, ?, 'compra', datetime('now'), ?)
    """, (uid, total, productos_str))

    conn.commit()

    # 📡 Enviar a Arduino (FORMATO CORRECTO)
    if arduino:
        nombre_lcd = formatear_nombre_lcd(nombre)
        arduino.write(f"OK:{nombre_lcd}:{round(nuevo,2)}\n".encode())

    # 🔄 Limpiar venta
    venta_data = {}

    return jsonify({
        "mensaje": f"✅ Compra realizada. Nuevo saldo: ${round(nuevo,2)}"
    })

# ==============================
# recarga
# ==============================
@app.route("/iniciar_recarga", methods=["POST"])
def iniciar_recarga():

    if not requiere_login("admin"):
        return jsonify({"mensaje":"No autorizado"}), 403

    session["modo_recarga"] = True
    session["uid_recarga"] = None

    return jsonify({"ok": True})

@app.route("/guardar_recarga", methods=["POST"])
def guardar_recarga():
    conn, cursor = get_db()
    global uid_recarga

    data = request.json
    monto = float(data.get("monto", 0))

    # ❌ Validar monto
    if monto <= 0:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ Monto inválido"})

    # ❌ No hay tarjeta
    if not uid_recarga:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ No se detectó tarjeta"})

    # 🔍 Buscar tarjeta
    cursor.execute("SELECT saldo, bloqueada FROM tarjetas WHERE uid=?", (uid_recarga,))
    r = cursor.fetchone()

    # ❌ No existe
    if not r:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "❌ Tarjeta no registrada"})

    saldo_actual = r[0]
    bloqueada = r[1]

    # 🔒 Tarjeta bloqueada
    if bloqueada:
        if arduino:
            arduino.write(b"ERROR\n")
        return jsonify({"mensaje": "🔒 Tarjeta bloqueada. No se puede recargar hasta desbloquearla."})

    nuevo = saldo_actual + monto

    # 💾 Guardar
    cursor.execute(
        "UPDATE tarjetas SET saldo=? WHERE uid=?",
        (nuevo, uid_recarga)
    )

    # 🧾 (opcional) historial
    cursor.execute("""
        INSERT INTO historial(uid, pago, movimiento, fecha)
        VALUES (?, ?, 'recarga', datetime('now'))
    """, (uid_recarga, monto))
    conn.commit()

    # 📡 Arduino
    if arduino:
        arduino.write(f"RECARGA:{round(nuevo,2)}\n".encode())

    # 🔄 Reset UID
    uid_recarga = None

    return jsonify({
        "mensaje": f"✅ Recarga exitosa. Nuevo saldo: ${round(nuevo,2)}"
    })

@app.route("/historial/<uid>")
def obtener_historial(uid):
    conn, cursor = get_db()
    cursor.execute("""
        SELECT pago, movimiento, fecha
        FROM historial
        WHERE uid=?
        ORDER BY fecha DESC
    """, (uid.strip(),))
    
    datos = cursor.fetchall()

    resultado = []
    for d in datos:
        resultado.append({
            "pago": d[0],
            "movimiento": d[1],
            "fecha": d[2]
        })

    return jsonify(resultado)

@app.route("/historial_admin")
def historial_admin():
    conn, cursor = get_db()

    if not requiere_login("admin"):
        return jsonify([]), 403

    cursor.execute("""
        SELECT h.id,
               h.uid,
               COALESCE(t.nombre, u.nombres || ' ' || u.apellidos) as nombre,
               h.pago,
               h.detalle,
               h.movimiento,
               h.fecha
        FROM historial h
        LEFT JOIN tarjetas t ON h.uid = t.uid
        LEFT JOIN usuarios u ON h.uid = u.uid
        ORDER BY h.id DESC
        LIMIT 100
    """)

    datos = cursor.fetchall()

    resultado = []

    for d in datos:
        resultado.append({
            "id": d[0],
            "uid": d[1],
            "nombre": d[2] or "Sin nombre",
            "pago": float(d[3]),
            "detalle": d[4],
            "movimiento": d[5],
            "fecha": d[6]
        })

    return jsonify(resultado)

@app.route("/buscar_movimiento/<int:id_mov>")
def buscar_movimiento(id_mov):
    conn, cursor = get_db()

    if not requiere_login():
        return jsonify({"ok": False}), 403

    cursor.execute("""
        SELECT h.id,
               h.uid,
               COALESCE(t.nombre, u.nombres || ' ' || u.apellidos) as nombre,
               h.pago,
               h.movimiento,
               h.detalle,
               h.fecha
        FROM historial h
        LEFT JOIN tarjetas t ON h.uid = t.uid
        LEFT JOIN usuarios u ON h.uid = u.uid
        WHERE h.id=?
    """, (id_mov,))

    r = cursor.fetchone()

    if not r:
        return jsonify({"ok": False})

    return jsonify({
        "ok": True,
        "id": r[0],
        "uid": r[1],
        "nombre": r[2] or "Sin nombre",
        "pago": float(r[3]),
        "movimiento": r[4],
        "detalle": r[5],
        "fecha": r[6]
    })

@app.route("/revertir", methods=["POST"])
def revertir():
    conn, cursor = get_db()
    if not session.get("login") or session.get("tipo") != "admin":
        return jsonify({"mensaje": "❌ No autorizado"}), 403

    data = request.json
    id_mov = data.get("id")

    # 🔍 buscar movimiento
    cursor.execute("""
    SELECT uid, pago, movimiento, detalle
    FROM historial
    WHERE id=?
    """, (id_mov,))
    
    r = cursor.fetchone()

    if not r:
        return jsonify({"mensaje": "❌ ID no encontrado"})

    uid, pago, movimiento, detalle = r

    # 🔥 verificar si ya fue revertido
    if detalle and "REVERSO" in detalle:
        return jsonify({"mensaje": "⚠️ Ya fue revertido"})

    # 🔍 saldo actual
    cursor.execute("SELECT saldo, bloqueada FROM tarjetas WHERE uid=?", (uid,))
    s = cursor.fetchone()

    if not s:
        return jsonify({"mensaje": "❌ Usuario no encontrado"})

    # 🔒 Tarjeta bloqueada
    if s[1]:
        return jsonify({"mensaje": "🔒 No se puede revertir: tarjeta bloqueada."})

    saldo = s[0]

    if movimiento == "compra":
        nuevo = saldo + pago

    elif movimiento == "recarga":
        if saldo < pago:
            return jsonify({"mensaje": "❌ No se puede revertir (saldo insuficiente)"})
        nuevo = saldo - pago

    else:
        return jsonify({"mensaje": "❌ Movimiento no válido"})

    # 💾 actualizar saldo
    cursor.execute(
        "UPDATE tarjetas SET saldo=? WHERE uid=?",
        (nuevo, uid)
    )

    # 🧾 guardar reverso
    cursor.execute("""
    INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
    VALUES (?, ?, 'reverso', datetime('now'), ?)
    """, (
        uid,
        pago,
        f"REVERSO de ID {id_mov}"
    ))

    conn.commit()

    return jsonify({
        "mensaje": f"✅ Transacción revertida. Nuevo saldo: ${round(nuevo,2)}"
    })

@app.route("/ranking")
def ranking():
    conn, cursor = get_db()

    cursor.execute("""
        SELECT 
            u.uid,
            u.nombres || ' ' || u.apellidos AS nombre,
            COALESCE(p.puntos, 0) AS puntos
        FROM usuarios u
        LEFT JOIN puntos p ON u.uid = p.uid
        ORDER BY puntos DESC
    """)

    datos = cursor.fetchall()

    ranking_completo = []
    for i, (uid, nombre, puntos) in enumerate(datos):
        ranking_completo.append({
            "posicion": i + 1,
            "uid": uid,
            "nombre": nombre,
            "puntos": float(puntos)
        })

    # Top 5
    top5 = ranking_completo[:5]

    # Si viene uid del cliente, agregar su posición si no está en top 5
    uid_cliente = request.args.get("uid", "")
    yo = None
    if uid_cliente:
        en_top5 = any(r["uid"] == uid_cliente for r in top5)
        if not en_top5:
            for r in ranking_completo:
                if r["uid"] == uid_cliente:
                    yo = r
                    break

    return jsonify({"top5": top5, "yo": yo})

# ==============================
# usuarios
# ==============================
@app.route("/consultar_usuario/<uid>")
def consultar_usuario(uid):
    conn, cursor = get_db()
    cursor.execute("SELECT * FROM usuarios WHERE uid=?", (uid,))
    u = cursor.fetchone()

    if not u:
        return jsonify({
            "uid": uid,
            "nombres": "N/A",
            "apellidos": "N/A",
            "correo": "N/A",
            "telefono": "N/A",
            "institucion": "N/A",
            "curso": "N/A"
        })

    return jsonify({
        "uid": u[0],
        "nombres": u[1] or "N/A",
        "apellidos": u[2] or "N/A",
        "correo": u[3] or "N/A",
        "telefono": u[4] or "N/A",
        "institucion": u[5] or "N/A",
        "curso": u[6] or "N/A"
    })


@app.route("/buscar_usuario_nombre/<nombre>")
def buscar_usuario_nombre(nombre):
    conn, cursor = get_db()
    cursor.execute("SELECT uid,nombres,apellidos FROM usuarios WHERE nombres LIKE ?", (f"%{nombre}%",))
    resultados = cursor.fetchall()

    lista = []
    for r in resultados:
        lista.append({
            "uid": r[0],
            "nombres": r[1],
            "apellidos": r[2]
        })

    return jsonify(lista)


@app.route("/guardar_usuario", methods=["POST"])
def guardar_usuario():
    conn, cursor = get_db()
    data = request.json
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("""
    INSERT INTO usuarios(uid,nombres,apellidos,correo,telefono,institucion,curso,fecha_registro)
    VALUES(?,?,?,?,?,?,?,?)
    ON CONFLICT(uid) DO UPDATE SET
        nombres=?, apellidos=?, correo=?, telefono=?, institucion=?, curso=?
    """, (
        data["uid"], data["nombres"], data["apellidos"], data["correo"],
        data["telefono"], data["institucion"], data["curso"], fecha,
        data["nombres"], data["apellidos"], data["correo"],
        data["telefono"], data["institucion"], data["curso"]
    ))

    # sincronizar tarjetas
    nombre_completo = f"{data['nombres']} {data['apellidos']}"

    cursor.execute("""
    INSERT INTO tarjetas(uid,nombre,saldo)
    VALUES(?,?,0)
    ON CONFLICT(uid) DO UPDATE SET nombre=?
    """, (data["uid"], nombre_completo, nombre_completo))

    conn.commit()

    return jsonify({"mensaje": "✅ Usuario guardado"})


@app.route("/bloquear_tarjeta", methods=["POST"])
def bloquear_tarjeta():
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "❌ No autorizado"}), 403

    data = request.json
    uid = data.get("uid", "").strip()
    motivo = data.get("motivo", "robo/pérdida").strip()

    if not uid:
        return jsonify({"ok": False, "mensaje": "❌ UID requerido"})

    cursor.execute("SELECT uid FROM tarjetas WHERE uid=?", (uid,))
    if not cursor.fetchone():
        return jsonify({"ok": False, "mensaje": "❌ Tarjeta no encontrada"})

    cursor.execute("UPDATE tarjetas SET bloqueada=1 WHERE uid=?", (uid,))

    # Registrar en historial
    cursor.execute("""
        INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
        VALUES (?, 0, 'bloqueo', datetime('now'), ?)
    """, (uid, f"Tarjeta bloqueada: {motivo}"))

    conn.commit()
    return jsonify({"ok": True, "mensaje": f"🔒 Tarjeta bloqueada correctamente ({motivo})"})


@app.route("/desbloquear_tarjeta", methods=["POST"])
def desbloquear_tarjeta():
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "❌ No autorizado"}), 403

    data = request.json
    uid = data.get("uid", "").strip()

    if not uid:
        return jsonify({"ok": False, "mensaje": "❌ UID requerido"})

    cursor.execute("SELECT uid FROM tarjetas WHERE uid=?", (uid,))
    if not cursor.fetchone():
        return jsonify({"ok": False, "mensaje": "❌ Tarjeta no encontrada"})

    cursor.execute("UPDATE tarjetas SET bloqueada=0 WHERE uid=?", (uid,))

    # Registrar en historial
    cursor.execute("""
        INSERT INTO historial(uid, pago, movimiento, fecha, detalle)
        VALUES (?, 0, 'desbloqueo', datetime('now'), 'Tarjeta desbloqueada por administrador')
    """, (uid,))

    conn.commit()
    return jsonify({"ok": True, "mensaje": "🔓 Tarjeta desbloqueada correctamente"})


@app.route("/estado_tarjeta/<uid>")
def estado_tarjeta(uid):
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "No autorizado"}), 403

    cursor.execute("""
        SELECT t.bloqueada, u.nombres, u.apellidos
        FROM tarjetas t
        LEFT JOIN usuarios u ON t.uid = u.uid
        WHERE t.uid=?
    """, (uid,))
    r = cursor.fetchone()

    if not r:
        return jsonify({"ok": False, "mensaje": "❌ Tarjeta no encontrada"})

    return jsonify({
        "ok": True,
        "bloqueada": bool(r[0]),
        "nombre": f"{r[1] or ''} {r[2] or ''}".strip() or "Sin nombre"
    })


@app.route("/listar_usuarios_con_estado")
def listar_usuarios_con_estado():
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify([]), 403

    orden = request.args.get("orden", "fecha_desc")

    if orden == "fecha_asc":
        query = "ORDER BY u.fecha_registro ASC"
    elif orden == "fecha_desc":
        query = "ORDER BY u.fecha_registro DESC"
    elif orden == "uid":
        query = "ORDER BY u.uid ASC"
    elif orden == "nombre":
        query = "ORDER BY u.apellidos ASC, u.nombres ASC"
    else:
        query = ""

    cursor.execute(f"""
    SELECT u.uid, u.nombres, u.apellidos, u.correo, u.telefono,
           u.institucion, u.curso, u.fecha_registro,
           COALESCE(t.bloqueada, 0) as bloqueada
    FROM usuarios u
    LEFT JOIN tarjetas t ON u.uid = t.uid
    {query}
    """)

    rows = cursor.fetchall()
    lista = []
    for r in rows:
        lista.append({
            "uid": r[0],
            "nombres": r[1],
            "apellidos": r[2],
            "correo": r[3],
            "telefono": r[4],
            "institucion": r[5],
            "curso": r[6],
            "fecha": r[7],
            "bloqueada": bool(r[8])
        })

    return jsonify(lista)



    conn, cursor = get_db()
    orden = request.args.get("orden", "fecha_desc")

    if orden == "fecha_asc":
        query = "ORDER BY fecha_registro ASC"
    elif orden == "fecha_desc":
        query = "ORDER BY fecha_registro DESC"
    elif orden == "uid":
        query = "ORDER BY uid ASC"
    elif orden == "nombre":
        query = "ORDER BY apellidos ASC, nombres ASC"
    else:
        query = ""

    cursor.execute(f"""
    SELECT uid,nombres,apellidos,correo,telefono,institucion,curso,fecha_registro
    FROM usuarios {query}
    """)

    rows = cursor.fetchall()

    lista = []
    for r in rows:
        lista.append({
            "uid": r[0],
            "nombres": r[1],
            "apellidos": r[2],
            "correo": r[3],
            "telefono": r[4],
            "institucion": r[5],
            "curso": r[6],
            "fecha": r[7]
        })

    return jsonify(lista)

@app.route("/guardar_ahorro", methods=["POST"])
def guardar_ahorro():
    conn, cursor = get_db()
    uid = session.get("uid")

    if not uid:
        return jsonify({"mensaje": "❌ Usuario no autenticado"}), 403

    # 🔒 Verificar si la tarjeta está bloqueada
    db_conn, db_cur = get_db()
    db_cur.execute("SELECT bloqueada FROM tarjetas WHERE uid=?", (uid,))
    tar = db_cur.fetchone()
    if tar and tar[0]:
        return jsonify({"mensaje": "🔒 Tarjeta bloqueada. No se puede modificar la configuración."}), 403

    data = request.json or {}

    modo = data.get("modo") or "porcentaje"
    valor = data.get("valor")

    if valor is None:
        return jsonify({"mensaje": "❌ Datos incompletos"}), 400

    valor = float(valor)

    # guardar configuración REAL
    cursor.execute("""
    INSERT INTO configuracion_ahorro(uid, modo, valor)
    VALUES(?,?,?)
    ON CONFLICT(uid) DO UPDATE SET modo=?, valor=?
    """, (uid, modo, valor, modo, valor))

    conn.commit()

    return jsonify({
        "mensaje": f"✅ Configuración guardada ({modo}: {valor})"
    })

@app.route("/guardar_meta", methods=["POST"])
def guardar_meta():
    conn, cursor = get_db()
    uid = session.get("uid")

    if not uid:
        return jsonify({"mensaje": "❌ No autenticado"}), 403

    # 🔒 Verificar si la tarjeta está bloqueada
    db_conn2, db_cur2 = get_db()
    db_cur2.execute("SELECT bloqueada FROM tarjetas WHERE uid=?", (uid,))
    tar2 = db_cur2.fetchone()
    if tar2 and tar2[0]:
        return jsonify({"mensaje": "🔒 Tarjeta bloqueada. No se puede modificar la meta."}), 403

    data = request.json
    meta = float(data.get("meta", 0))

    cursor.execute("""
    INSERT INTO metas_ahorro(uid, meta)
    VALUES(?,?)
    ON CONFLICT(uid) DO UPDATE SET meta=?
    """, (uid, meta, meta))

    conn.commit()

    return jsonify({"mensaje": "✅ Meta guardada"})

# ==============================
# rfid thread
# ==============================
def leer_rfid():
    while True:
        try:
            if arduino:
                linea = arduino.readline().decode(errors='ignore').strip().upper()

                if linea and linea.isalnum() and 8 <= len(linea) <= 20:
                    
                    # 🔥 evitar saturación
                    if cola_uid.qsize() < 5:
                        cola_uid.put(linea)

        except Exception as e:
            print("Error serial:", e)
            time.sleep(1)

threading.Thread(target=leer_rfid, daemon=True).start()

# ==============================
# cambiar credenciales cliente
# ==============================
@app.route("/cambiar_credenciales", methods=["POST"])
def cambiar_credenciales():
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "❌ No autorizado"}), 403

    data = request.json
    uid = data.get("uid", "").strip()
    nuevo_user = data.get("user", "").strip()
    nuevo_pass = data.get("pass", "").strip()

    if not uid or not nuevo_user or not nuevo_pass:
        return jsonify({"ok": False, "mensaje": "❌ Datos incompletos"})

    # Verificar que el cliente existe
    cursor.execute("SELECT uid FROM usuarios WHERE uid=?", (uid,))
    if not cursor.fetchone():
        return jsonify({"ok": False, "mensaje": "❌ UID no encontrado"})

    # Guardar en tabla credenciales_cliente (crearla si no existe)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credenciales_cliente (
            uid TEXT PRIMARY KEY,
            usuario TEXT,
            contrasena TEXT
        )
    """)

    cursor.execute("""
        INSERT INTO credenciales_cliente (uid, usuario, contrasena)
        VALUES (?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET usuario=?, contrasena=?
    """, (uid, nuevo_user, nuevo_pass, nuevo_user, nuevo_pass))

    conn.commit()

    # Invalidar sesión activa del estudiante si está logueado
    _sesiones_invalidadas.add(uid)

    return jsonify({"ok": True, "mensaje": f"✅ Credenciales actualizadas para UID {uid}"})


# ==============================
# consultar ahorro (admin)
# ==============================
@app.route("/consultar_ahorro_admin/<uid>")
def consultar_ahorro_admin(uid):
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "No autorizado"}), 403

    cursor.execute("SELECT nombres, apellidos FROM usuarios WHERE uid=?", (uid,))
    u = cursor.fetchone()
    if not u:
        return jsonify({"ok": False, "mensaje": "UID no encontrado"})

    nombre = f"{u[0]} {u[1]}"

    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM ahorros WHERE uid=?", (uid,))
    ahorro = float(cursor.fetchone()[0])

    return jsonify({"ok": True, "nombre": nombre, "ahorro": ahorro})


# ==============================
# retiro de ahorro (admin)
# ==============================
@app.route("/procesar_retiro", methods=["POST"])
def procesar_retiro():
    conn, cursor = get_db()
    if not requiere_login("admin"):
        return jsonify({"ok": False, "mensaje": "❌ No autorizado"}), 403

    data = request.json
    uid = data.get("uid", "").strip()
    monto = float(data.get("monto", 0))

    if not uid or monto <= 0:
        return jsonify({"ok": False, "mensaje": "❌ Datos inválidos"})

    # 🔒 Verificar si la tarjeta está bloqueada
    cursor.execute("SELECT bloqueada FROM tarjetas WHERE uid=?", (uid,))
    tar = cursor.fetchone()
    if tar and tar[0]:
        return jsonify({"ok": False, "mensaje": "🔒 Tarjeta bloqueada. No se puede procesar el retiro."})

    # Verificar ahorro disponible
    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM ahorros WHERE uid=?", (uid,))
    ahorro_total = float(cursor.fetchone()[0])

    if monto > ahorro_total:
        return jsonify({"ok": False, "mensaje": f"❌ Monto supera el ahorro disponible (${ahorro_total:.2f})"})

    # Registrar retiro como monto negativo en ahorros
    now = datetime.now()
    semana = f"{now.year}-{now.isocalendar()[1]}"

    cursor.execute("""
        INSERT INTO ahorros (uid, semana, monto, tipo)
        VALUES (?, ?, ?, 'ahorro')
    """, (uid, semana, -monto))

    cursor.execute("""
        INSERT INTO historial (uid, pago, movimiento, fecha, detalle)
        VALUES (?, ?, 'retiro', datetime('now'), 'Retiro de ahorro')
    """, (uid, monto))

    conn.commit()

    nuevo_ahorro = ahorro_total - monto

    return jsonify({"ok": True, "mensaje": f"✅ Retiro de ${monto:.2f} procesado. Ahorro restante: ${nuevo_ahorro:.2f}"})


# ==============================
# pa que corra la app
# ==============================
if __name__ == "__main__":
    app.run(debug=False)