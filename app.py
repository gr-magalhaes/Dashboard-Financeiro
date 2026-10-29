import hmac
import unicodedata
from datetime import timedelta

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title="Financeiro", page_icon="💰", layout="wide")

API = "https://api.clickup.com/api/v2"
TZ = "America/Sao_Paulo"
CORES = {"Recebido": "#2e9e5b", "Previsto": "#3b82f6", "Atrasado": "#e5484d"}
SITS = ["Recebido", "Previsto", "Atrasado"]
OUTROS = "Outros responsáveis"
SEM_RESP = "Sem responsável"


# ---------------------------------------------------------------- utilidades
def cfg(chave, padrao=None):
    try:
        return st.secrets[chave]
    except Exception:
        return padrao


def norm(texto):
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.strip().lower()


def norm_tag(texto):
    return " ".join(norm(texto).replace("-", " ").replace("_", " ").split())


def brl(v):
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


MESES = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


def aviso(chave, texto, tipo="warning"):
    """Mostra um aviso com botão ✕ para fechar (lembrado enquanto a página estiver aberta)."""
    if st.session_state.get(f"fechado_{chave}"):
        return
    col_msg, col_btn = st.columns([30, 1], vertical_alignment="center")
    with col_msg:
        getattr(st, tipo)(texto)
    with col_btn:
        if st.button("✕", key=f"btn_{chave}", help="Fechar aviso"):
            st.session_state[f"fechado_{chave}"] = True
            st.rerun()


def senha_ok():
    senha = cfg("APP_PASSWORD")
    if not senha:
        return True
    if st.session_state.get("auth"):
        return True
    digitada = st.text_input("Senha", type="password")
    if digitada:
        if hmac.compare_digest(digitada, senha):
            st.session_state["auth"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    return False


if not senha_ok():
    st.stop()


# ------------------------------------------------------------------- ClickUp
@st.cache_data(ttl=300, show_spinner="Buscando dados do ClickUp...")
def buscar_tarefas(token, list_ids):
    headers = {"Authorization": token}
    tarefas = []
    for list_id in list_ids:
        pagina = 0
        while True:
            r = requests.get(
                f"{API}/list/{list_id}/task",
                headers=headers,
                params={
                    "page": pagina,
                    "include_closed": "true",
                    "subtasks": "false",
                },
                timeout=30,
            )
            r.raise_for_status()
            dados = r.json()
            lote = dados.get("tasks", [])
            tarefas.extend(lote)
            if not lote or dados.get("last_page", len(lote) < 100):
                break
            pagina += 1
    return tarefas


@st.cache_data(ttl=3600, show_spinner="Mapeando listas do Space...")
def listas_dos_espacos(token, space_ids):
    headers = {"Authorization": token}
    ids = []
    for sid in space_ids:
        r = requests.get(
            f"{API}/space/{sid}/list",
            headers=headers,
            params={"archived": "false"},
            timeout=30,
        )
        r.raise_for_status()
        ids += [l["id"] for l in r.json().get("lists", [])]
        r = requests.get(
            f"{API}/space/{sid}/folder",
            headers=headers,
            params={"archived": "false"},
            timeout=30,
        )
        r.raise_for_status()
        for pasta in r.json().get("folders", []):
            ids += [l["id"] for l in pasta.get("lists", [])]
    return ids


def campo(tarefa, nome):
    for f in tarefa.get("custom_fields", []):
        if norm(f.get("name", "")) == norm(nome):
            return f.get("value")
    return None


def nomes_de_campos(tarefas):
    nomes = set()
    for t in tarefas:
        for f in t.get("custom_fields", []):
            nomes.add(f.get("name", ""))
    return sorted(nomes)


def para_float(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def para_bool(v):
    return v in (True, 1, "true", "True", "1")


def preenchido(v):
    return v not in (None, "", False, 0, "0", "false", "False")


def para_data(v):
    if v in (None, ""):
        return pd.NaT
    try:
        ts = pd.to_datetime(int(v), unit="ms", utc=True)
        return ts.tz_convert(TZ).tz_localize(None).normalize()
    except (ValueError, TypeError):
        return pd.NaT


def montar_df(tarefas, f_valor, f_nf, f_pago, f_data, tag_ignorar, tag_fora):
    hoje = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
    linhas = []
    for t in tarefas:
        tags = {norm_tag(x.get("name", "")) for x in t.get("tags", [])}
        resp = [
            (a.get("username") or a.get("email") or "").strip()
            for a in t.get("assignees", [])
        ]
        resp = [r for r in resp if r]
        pago = para_bool(campo(t, f_pago))
        data = para_data(campo(t, f_data))
        origem = "Data de pagamento" if not pd.isna(data) else ""
        if pd.isna(data) and not pago:
            # previsão sem data de pagamento: usa a due date da tarefa
            due = para_data(t.get("due_date"))
            if not pd.isna(due):
                data, origem = due, "Due date"
        if pd.isna(data):
            situacao = "Sem data"
        elif pago:
            situacao = "Recebido"
        elif data < hoje:
            situacao = "Atrasado"
        else:
            situacao = "Previsto"
        linhas.append(
            {
                "Tarefa": t.get("name", ""),
                "Status": (t.get("status") or {}).get("status", ""),
                "Valor": para_float(campo(t, f_valor)),
                "NF": preenchido(campo(t, f_nf)),
                "Pago": pago,
                "Data": data,
                "Origem da data": origem,
                "Situação": situacao,
                "Responsáveis": resp,
                "Tags": sorted(tags),
                "Fora do exp": any(tag_fora in x for x in tags),
                "Ignorar": any(tag_ignorar in x for x in tags),
                "Link": t.get("url", ""),
            }
        )
    df = pd.DataFrame(linhas)
    if not df.empty:
        df["Data"] = pd.to_datetime(df["Data"])
    return df


# ------------------------------------------------------------------ cálculos
def socio_de(nome, socios):
    n = norm(nome)
    for s in socios:
        if norm(s) in n:
            return s
    return None


def calcular(df, imp, cx, pct_direto, socios):
    """Impostos, caixa, líquido e divisão entre sócios, tarefa por tarefa."""
    df = df.copy().reset_index(drop=True)
    df["Impostos"] = df["Valor"] * imp
    df["Caixa"] = df["Valor"] * cx
    df["Líquido"] = df["Valor"] - df["Impostos"] - df["Caixa"]
    destinos = list(socios) + [OUTROS, SEM_RESP]
    partes = []
    for _, r in df.iterrows():
        p = dict.fromkeys(destinos, 0.0)
        direto = r["Líquido"] * pct_direto if r["Fora do exp"] else 0.0
        pool = r["Líquido"] - direto
        for s in socios:
            p[s] += pool / len(socios)
        if direto:
            resp = r["Responsáveis"]
            if not resp:
                p[SEM_RESP] += direto
            else:
                for nome in resp:
                    p[socio_de(nome, socios) or OUTROS] += direto / len(resp)
        partes.append(p)
    partes_df = pd.DataFrame(partes, columns=destinos)
    return pd.concat([df, partes_df], axis=1)


def colunas_socios(df, socios):
    cols = list(socios)
    for extra in (OUTROS, SEM_RESP):
        if extra in df.columns and df[extra].abs().sum() > 0.005:
            cols.append(extra)
    return cols


def formata_dinheiro(g, cols):
    out = g.copy()
    for c in cols:
        out[c] = out[c].apply(brl)
    return out


def tabela_tarefas(det, cols_socios, crescente=True):
    det = det.sort_values("Data", ascending=crescente).copy()
    det["Data"] = det["Data"].dt.strftime("%d/%m/%Y")
    det["Responsáveis"] = det["Responsáveis"].apply(lambda r: ", ".join(r))
    dinheiro = ["Valor", "Líquido"] + cols_socios
    cols = (
        ["Tarefa", "Status", "Situação", "Data", "Origem da data"]
        + dinheiro
        + ["Pago", "NF", "Fora do exp", "Responsáveis", "Link"]
    )
    st.dataframe(
        formata_dinheiro(det, dinheiro)[cols],
        hide_index=True,
        width="stretch",
        column_config={"Link": st.column_config.LinkColumn("Link", display_text="abrir")},
    )


def painel_tarefas(det, titulo, cols_socios):
    with st.container(border=True):
        st.markdown(
            f"**Tarefas — {titulo}**  \n"
            f"{len(det)} tarefa(s) · bruto {brl(det['Valor'].sum())} · "
            f"líquido {brl(det['Líquido'].sum())}"
        )
        tabela_tarefas(det, cols_socios)


def tabela_periodo(sub, coluna, rotulo, crescente, cols_socios, chave):
    if sub.empty:
        st.info("Nenhum lançamento no período selecionado.")
        return
    agg = {
        "Projetos": ("Valor", "size"),
        "Bruto": ("Valor", "sum"),
        "Impostos": ("Impostos", "sum"),
        "Caixa": ("Caixa", "sum"),
        "Líquido": ("Líquido", "sum"),
    }
    for c in cols_socios:
        agg[c] = (c, "sum")
    g = (
        sub.groupby(coluna)
        .agg(**agg)
        .reset_index()
        .sort_values(coluna, ascending=crescente)
    )
    periodos = list(g[coluna])
    dinheiro = ["Bruto", "Impostos", "Caixa", "Líquido"] + cols_socios
    total = {coluna: "TOTAL", "Projetos": g["Projetos"].sum()}
    for c in dinheiro:
        total[c] = g[c].sum()
    g[coluna] = g[coluna].apply(rotulo)
    g = pd.concat([g, pd.DataFrame([total])], ignore_index=True)
    g = g.rename(columns={coluna: "Período"})
    evento = st.dataframe(
        formata_dinheiro(g, dinheiro)[["Período", "Projetos"] + dinheiro],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=chave,
    )
    linhas = evento.selection.rows
    if not linhas:
        st.caption("👆 Clique numa linha para ver as tarefas que compõem o valor (TOTAL mostra todas).")
        return
    i = linhas[0]
    if i < len(periodos):
        det = sub[sub[coluna] == periodos[i]]
        titulo = rotulo(periodos[i])
    else:
        det = sub
        titulo = "todos os períodos da tabela"
    painel_tarefas(det, titulo, cols_socios)


def visao_periodo(df, coluna, rotulo, cols_socios):
    base = df.dropna(subset=["Data"])
    base = base[base["Situação"].isin(SITS)]
    if base.empty:
        st.info("Nenhum lançamento no período selecionado.")
        return

    st.subheader("Entrou (pagamentos recebidos)")
    tabela_periodo(
        base[base["Situação"] == "Recebido"],
        coluna,
        rotulo,
        False,
        cols_socios,
        f"{coluna}-recebido",
    )
    st.subheader("Previsão (previsto + atrasado)")
    tabela_periodo(
        base[base["Situação"].isin(["Previsto", "Atrasado"])],
        coluna,
        rotulo,
        True,
        cols_socios,
        f"{coluna}-previsao",
    )

    # gráfico por último
    st.subheader("Gráfico")
    graf = base.groupby([coluna, "Situação"], as_index=False)["Valor"].sum()
    graf["Período"] = graf[coluna].apply(rotulo)
    ordem = [rotulo(p) for p in sorted(graf[coluna].unique())]
    fig = px.bar(
        graf,
        x="Período",
        y="Valor",
        color="Situação",
        color_discrete_map=CORES,
        category_orders={"Período": ordem},
        labels={"Valor": "Bruto (R$)"},
    )
    fig.update_xaxes(type="category")
    fig.update_layout(legend_title_text="", margin=dict(t=20))
    st.plotly_chart(fig, width="stretch")


# ------------------------------------------------------------------ interface
st.title("💰 Financeiro")

token = cfg("CLICKUP_TOKEN")
list_ids = [x.strip() for x in str(cfg("CLICKUP_LIST_IDS", "")).split(",") if x.strip()]
space_ids = [x.strip() for x in str(cfg("CLICKUP_SPACE_IDS", "")).split(",") if x.strip()]
if not token or not (list_ids or space_ids):
    st.error(
        "Configure CLICKUP_TOKEN e CLICKUP_SPACE_IDS (ou CLICKUP_LIST_IDS) nos secrets do app."
    )
    st.stop()

F_VALOR = cfg("FIELD_VALOR", "Valor")
F_NF = cfg("FIELD_NF", cfg("FIELD_FATURADO", "NF"))
F_PAGO = cfg("FIELD_PAGO", "Pago")
F_DATA = cfg("FIELD_DATA_PAGAMENTO", "Data de Pagamento")
SOCIOS = [s.strip() for s in str(cfg("SOCIOS", "Gustavo,Lucas,Guilherme")).split(",") if s.strip()]
TAG_IGNORAR = norm_tag(cfg("TAG_IGNORAR", "pre-outubro"))
TAG_FORA = norm_tag(cfg("TAG_FORA_EXP", "fora do exp"))

with st.sidebar:
    st.header("Configurações")
    if st.button("🔄 Atualizar dados agora"):
        buscar_tarefas.clear()
        st.rerun()
    imp = st.number_input("Impostos (%)", 0.0, 100.0, 6.0, 0.5) / 100
    cx = st.number_input("Caixa (%)", 0.0, 100.0, 6.0, 0.5) / 100
    pct_direto = st.number_input("Fora do exp: direto ao responsável (%)", 0.0, 100.0, 30.0, 5.0) / 100
    st.caption(f"Líquido = bruto − {(imp + cx) * 100:.1f}%")

try:
    if space_ids:
        list_ids = sorted(set(list_ids) | set(listas_dos_espacos(token, tuple(space_ids))))
    tarefas = buscar_tarefas(token, tuple(list_ids))
except requests.HTTPError as e:
    st.error(f"Erro ao consultar o ClickUp: {e}")
    st.stop()
except requests.RequestException as e:
    st.error(f"Não foi possível conectar ao ClickUp: {e}")
    st.stop()

if not tarefas:
    st.warning("Nenhuma tarefa encontrada. Confira o(s) ID(s) do Space ou da lista.")
    st.stop()

existentes = {norm(n) for n in nomes_de_campos(tarefas)}
faltando = [n for n in (F_VALOR, F_PAGO, F_DATA) if norm(n) not in existentes]
if faltando:
    st.error(f"Campos não encontrados no ClickUp: {', '.join(faltando)}")
    st.write("Campos personalizados encontrados:", nomes_de_campos(tarefas))
    st.caption("Ajuste FIELD_VALOR, FIELD_PAGO e FIELD_DATA_PAGAMENTO nos secrets.")
    st.stop()

df = montar_df(tarefas, F_VALOR, F_NF, F_PAGO, F_DATA, TAG_IGNORAR, TAG_FORA)

contagem_tags = {}
for lista in df["Tags"]:
    for x in lista:
        contagem_tags[x] = contagem_tags.get(x, 0) + 1
n_ignoradas = int(df["Ignorar"].sum())
df = df[~df["Ignorar"]]

with st.sidebar:
    status_existentes = sorted(df["Status"].unique())
    padrao_excluir = [s for s in status_existentes if "orcando" in norm(s)]
    excluir = st.multiselect("Ignorar status", status_existentes, default=padrao_excluir)
    hoje = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
    ini_padrao = (hoje.replace(day=1) - pd.DateOffset(months=2)).date()
    fim_padrao = (hoje.replace(day=1) + pd.DateOffset(months=4) - timedelta(days=1)).date()
    periodo = st.date_input("Período", (ini_padrao, fim_padrao), format="DD/MM/YYYY")
    with st.expander("Diagnóstico de tags"):
        st.caption(
            f"Procurando por \"{TAG_FORA}\" (fora do exp) e \"{TAG_IGNORAR}\" (ignorar). "
            "Acentos, maiúsculas e hífens não importam."
        )
        if contagem_tags:
            st.write({k: v for k, v in sorted(contagem_tags.items())})
        else:
            st.write("Nenhuma tarefa retornada pelo ClickUp tem tags.")
        st.caption(f"Tarefas \"fora do exp\" reconhecidas: {int(df['Fora do exp'].sum())}")
    if n_ignoradas:
        st.caption(f"{n_ignoradas} tarefa(s) ignorada(s) pela tag \"{cfg('TAG_IGNORAR', 'pre-outubro')}\".")
    if any(v for k, v in st.session_state.items() if str(k).startswith("fechado_")):
        if st.button("Reexibir avisos fechados"):
            for k in [k for k in st.session_state if str(k).startswith("fechado_")]:
                del st.session_state[k]
            st.rerun()

df = df[~df["Status"].isin(excluir)]
if df.empty:
    st.warning("Nenhuma tarefa restante depois dos filtros.")
    st.stop()

df = calcular(df, imp, cx, pct_direto, SOCIOS)
cols_socios = colunas_socios(df, SOCIOS)

if isinstance(periodo, tuple) and len(periodo) == 2:
    p_ini, p_fim = pd.Timestamp(periodo[0]), pd.Timestamp(periodo[1])
else:
    p_ini, p_fim = pd.Timestamp(ini_padrao), pd.Timestamp(fim_padrao)

sem_data = df[df["Situação"] == "Sem data"]
df_per = df[(df["Data"] >= p_ini) & (df["Data"] <= p_fim)].copy()

if not sem_data.empty:
    aviso(
        "sem_data",
        f"{len(sem_data)} tarefa(s) sem data, fora dos cálculos "
        f"(soma: {brl(sem_data['Valor'].sum())}). São tarefas pagas sem data de "
        "pagamento, ou não pagas sem data de pagamento e sem due date.",
    )
usa_due = df[df["Origem da data"] == "Due date"]
if not usa_due.empty:
    aviso(
        "usa_due",
        f"{len(usa_due)} tarefa(s) não paga(s) sem data de pagamento usam a due date "
        f"como previsão (soma: {brl(usa_due['Valor'].sum())}).",
        "info",
    )
sem_valor = df[df["Valor"] == 0]
if not sem_valor.empty:
    aviso("sem_valor", f"{len(sem_valor)} tarefa(s) com valor zerado ou vazio.")
fora = df[df["Fora do exp"]]
if not fora.empty:
    sem_resp = fora[fora["Responsáveis"].apply(len) == 0]
    if not sem_resp.empty:
        aviso(
            "sem_resp",
            f"{len(sem_resp)} tarefa(s) \"fora do exp\" sem responsável: os "
            f"{pct_direto * 100:.0f}% diretos aparecem em \"{SEM_RESP}\".",
        )
    if df[OUTROS].abs().sum() > 0.005:
        aviso(
            "outros_resp",
            "Há tarefas \"fora do exp\" cujo responsável não é um dos sócios; "
            f"o valor direto aparece em \"{OUTROS}\".",
            "info",
        )

# semana: sexta a quinta
df_per["Semana"] = df_per["Data"] - pd.to_timedelta((df_per["Data"].dt.weekday - 4) % 7, unit="D")
df_per["Mês"] = df_per["Data"].dt.to_period("M").dt.to_timestamp()

# ano: considera todos os dados, sem o filtro de período
df_ano = df.dropna(subset=["Data"]).copy()
df_ano["Ano"] = pd.to_datetime(df_ano["Data"].dt.year.astype(str) + "-01-01")

rot_semana = lambda d: f"{d:%d/%m} – {(d + timedelta(days=6)):%d/%m/%y}"
rot_mes = lambda d: f"{MESES[d.month - 1]} {d.year}"
rot_ano = lambda d: f"{d.year}"

aba_mes, aba_sem, aba_ano, aba_dados = st.tabs(
    ["Por mês", "Por semana (sex–qui)", "Visão geral (por ano)", "Dados"]
)

with aba_mes:
    st.caption(f"Período: {p_ini:%d/%m/%Y} a {p_fim:%d/%m/%Y} (ajuste na barra lateral).")
    visao_periodo(df_per, "Mês", rot_mes, cols_socios)

with aba_sem:
    st.caption(
        f"Cada semana vai de sexta a quinta. Período: {p_ini:%d/%m/%Y} a {p_fim:%d/%m/%Y}."
    )
    visao_periodo(df_per, "Semana", rot_semana, cols_socios)

with aba_ano:
    st.caption("Considera todos os anos, independente do filtro de período da barra lateral.")
    visao_periodo(df_ano, "Ano", rot_ano, cols_socios)

with aba_dados:
    tabela_tarefas(df, cols_socios, crescente=False)
