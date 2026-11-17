/* Padrao da Central de Automações: tema claro/escuro unico para todas as telas + avisos rapidos.
   Carregar no <head> (sem defer) para aplicar o tema antes de desenhar e nao piscar.
   Botao de tema: qualquer <button data-tema></button> e preenchido e ligado sozinho.
   Aviso rapido: marca.avisar("Salvo.") / marca.avisar("Falhou", "bad") / marca.avisar("...", "info"). */
(function(){
  "use strict";
  var CHAVE = "central-tema";
  function ler(){
    try{
      var v = localStorage.getItem(CHAVE);
      if(v) return v;
      // quem ja tinha escolhido o escuro no Dashboard Diretoria ou na Central OPME continua no escuro
      v = localStorage.getItem("cockpit-tema") || localStorage.getItem("opme-tema");
      if(v){ localStorage.setItem(CHAVE, v); return v; }
    }catch(e){}
    return "light";
  }
  function aplicar(t){
    if(t === "dark") document.documentElement.setAttribute("data-theme", "dark");
    else document.documentElement.removeAttribute("data-theme");
  }
  aplicar(ler());

  var SOL = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var LUA = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg>';
  function escuro(){ return document.documentElement.getAttribute("data-theme") === "dark"; }
  function desenhar(b){
    var e = escuro();
    b.innerHTML = (e ? SOL : LUA) + '<span class="rotulo">' + (e ? "Claro" : "Escuro") + "</span>";
    b.setAttribute("aria-pressed", e ? "true" : "false");
    b.setAttribute("aria-label", e ? "Mudar para o tema claro" : "Mudar para o tema escuro");
  }
  var ouvintes = [];
  function trocar(){
    var t = escuro() ? "light" : "dark";
    aplicar(t);
    try{ localStorage.setItem(CHAVE, t); }catch(e){}
    document.querySelectorAll("[data-tema]").forEach(desenhar);
    ouvintes.forEach(function(f){ try{ f(t); }catch(e){} });
  }
  function ligar(){
    document.querySelectorAll("[data-tema]").forEach(function(b){
      if(b.dataset.temaLigado) return;
      b.dataset.temaLigado = "1"; b.classList.add("btn"); desenhar(b);
      b.addEventListener("click", trocar);
    });
  }
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", ligar); else ligar();

  function avisar(msg, tipo){
    var caixa = document.querySelector(".marca-toasts");
    if(!caixa){ caixa = document.createElement("div"); caixa.className = "marca-toasts"; caixa.setAttribute("aria-live", "polite"); document.body.appendChild(caixa); }
    var t = document.createElement("div");
    t.className = "marca-toast " + (tipo || ""); t.setAttribute("role", tipo === "bad" ? "alert" : "status"); t.textContent = msg;
    caixa.appendChild(t);
    setTimeout(function(){ t.remove(); }, tipo === "bad" ? 7000 : 4000);
  }

  window.marca = {avisar: avisar, aoTrocarTema: function(f){ ouvintes.push(f); }, escuro: escuro};
})();
