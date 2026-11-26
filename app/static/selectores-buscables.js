/* Búsqueda en los selectores existentes sin cambiar sus valores ni eventos. */
(function(){
  "use strict";
  var estilo=document.createElement("style");
  estilo.textContent=`
    .selector-buscable{position:relative;display:block;width:100%;min-width:0;font:inherit}
    .selector-buscable>select{position:absolute!important;width:1px!important;height:1px!important;opacity:0!important;pointer-events:none!important;left:0;top:0;padding:0!important;border:0!important}
    .selector-buscable-campo{display:flex;align-items:center;min-width:0;width:100%;border:1px solid #aebbc7;border-radius:9px;background:#fff;color:#0a2f43}
    .selector-buscable-campo:focus-within{outline:2px solid #0a2f43;outline-offset:1px}
    .selector-buscable-campo input{flex:1;min-width:0;width:100%;height:100%;min-height:39px;border:0!important;outline:0!important;background:transparent!important;color:inherit;font:inherit;padding:8px 10px!important;margin:0!important;box-shadow:none!important}
    .selector-buscable-flecha{border:0;background:transparent;color:#506a78;font:inherit;cursor:pointer;padding:7px 10px;align-self:stretch}
    .selector-buscable-lista{position:absolute;z-index:1000;top:calc(100% + 4px);left:0;width:100%;max-height:240px;overflow:auto;border:1px solid #b8c6ce;border-radius:9px;background:#fff;box-shadow:0 9px 25px #0a2f4330}
    .selector-buscable-lista[hidden]{display:none!important}
    .selector-buscable-lista button{display:block;width:100%;border:0;border-bottom:1px solid #e5ebef;border-radius:0;background:#fff;color:#0a2f43;text-align:left;font:inherit;font-size:.85rem;padding:9px 11px;cursor:pointer;white-space:normal;overflow-wrap:anywhere}
    .selector-buscable-lista button:hover,.selector-buscable-lista button:focus-visible,.selector-buscable-lista button[aria-selected=true]{background:#e7eef2}
    .selector-buscable-lista button:disabled{color:#80909b;cursor:not-allowed}
    .selector-buscable-vacio{padding:11px;color:#607988;font-size:.82rem}
    .selector-buscable.bloqueado .selector-buscable-campo{background:#eef1f3;opacity:.65}
    .filters>.selector-buscable{width:min(250px,100%);flex:0 1 250px}
    .toolbar>.selector-buscable{width:210px;min-width:170px}
  `;
  document.head.appendChild(estilo);

  function normalizar(texto){
    return String(texto||"").normalize("NFD").replace(/[\u0300-\u036f]/g,"").toLocaleLowerCase("es").trim();
  }
  function mejorar(select){
    if(select.dataset.buscableListo)return;
    select.dataset.buscableListo="1";
    var caja=document.createElement("span");caja.className="selector-buscable";
    select.parentNode.insertBefore(caja,select);caja.appendChild(select);
    var fila=document.createElement("span");fila.className="selector-buscable-campo";
    var campo=document.createElement("input");campo.type="search";campo.autocomplete="off";
    campo.setAttribute("role","combobox");campo.setAttribute("aria-autocomplete","list");
    campo.setAttribute("aria-expanded","false");
    var etiqueta=select.getAttribute("aria-label")||
      (select.id&&document.querySelector('label[for="'+select.id+'"]')?.textContent.trim())||
      Array.from(select.closest("label")?.childNodes||[]).filter(function(n){return n.nodeType===Node.TEXT_NODE})
        .map(function(n){return n.textContent.trim()}).join(" ").trim()||"Buscar opción";
    campo.setAttribute("aria-label",etiqueta);
    var flecha=document.createElement("button");flecha.type="button";flecha.className="selector-buscable-flecha";
    flecha.textContent="⌄";flecha.setAttribute("aria-label","Mostrar opciones");
    var lista=document.createElement("span");lista.className="selector-buscable-lista";lista.hidden=true;
    lista.id="opciones-"+(select.id||Math.random().toString(36).slice(2));
    campo.setAttribute("aria-controls",lista.id);
    fila.append(campo,flecha);caja.append(fila,lista);
    select.tabIndex=-1;select.setAttribute("aria-hidden","true");
    var buscando=false,indiceActivo=-1;
    function opciones(){return Array.from(select.options)}
    function sincronizar(){
      var elegida=select.selectedOptions[0];
      if(!buscando)campo.value=select.value&&elegida?elegida.textContent.trim():"";
      campo.placeholder=opciones().find(function(o){return !o.value})?.textContent.trim()||"Buscar…";
      var bloqueado=select.disabled;
      campo.disabled=bloqueado;flecha.disabled=bloqueado;caja.classList.toggle("bloqueado",bloqueado);
      if(bloqueado&&!lista.hidden)cerrar();
    }
    function cerrar(){
      lista.hidden=true;campo.setAttribute("aria-expanded","false");buscando=false;indiceActivo=-1;
      sincronizar();
    }
    function pintar(texto){
      lista.replaceChildren();
      var consulta=normalizar(texto);
      var encontrados=opciones().filter(function(o){
        return !consulta||normalizar(o.textContent+" "+o.value).includes(consulta);
      });
      if(!encontrados.length){
        var vacio=document.createElement("span");vacio.className="selector-buscable-vacio";
        vacio.textContent="Sin coincidencias";lista.appendChild(vacio);return;
      }
      encontrados.forEach(function(opcion){
        var boton=document.createElement("button");boton.type="button";
        boton.textContent=opcion.textContent.trim();boton.disabled=opcion.disabled;
        boton.dataset.valor=opcion.value;
        boton.setAttribute("aria-selected",String(opcion.value===select.value));
        boton.addEventListener("mousedown",function(e){e.preventDefault()});
        boton.addEventListener("click",function(){elegir(opcion.value)});
        lista.appendChild(boton);
      });
      indiceActivo=-1;
    }
    function abrir(){
      if(select.disabled)return;
      buscando=true;campo.value="";pintar("");lista.hidden=false;
      campo.setAttribute("aria-expanded","true");campo.focus();
    }
    function elegir(valor){
      if(!opciones().some(function(o){return o.value===valor&&!o.disabled}))return;
      var anterior=select.value;
      select.value=valor;
      cerrar();
      if(anterior!==valor){
        select.dispatchEvent(new Event("input",{bubbles:true}));
        select.dispatchEvent(new Event("change",{bubbles:true}));
      }
      campo.focus();
    }
    campo.addEventListener("focus",function(){if(!buscando)abrir()});
    campo.addEventListener("blur",function(){
      setTimeout(function(){if(!caja.contains(document.activeElement)&&!lista.hidden)cerrar()},0);
    });
    campo.addEventListener("input",function(){buscando=true;pintar(campo.value);lista.hidden=false;campo.setAttribute("aria-expanded","true")});
    campo.addEventListener("keydown",function(e){
      if(e.key==="Escape"){if(!lista.hidden){e.preventDefault();e.stopPropagation();cerrar()}return}
      if(e.key==="Tab"){cerrar();return}
      var botones=Array.from(lista.querySelectorAll("button:not(:disabled)"));
      if(e.key==="ArrowDown"||e.key==="ArrowUp"){
        e.preventDefault();if(lista.hidden)abrir();
        botones=Array.from(lista.querySelectorAll("button:not(:disabled)"));
        if(!botones.length)return;
        indiceActivo=(indiceActivo+(e.key==="ArrowDown"?1:-1)+botones.length)%botones.length;
        botones.forEach(function(b,i){b.setAttribute("aria-selected",String(i===indiceActivo))});
        botones[indiceActivo].scrollIntoView({block:"nearest"});
      }else if(e.key==="Enter"&&!lista.hidden){
        e.preventDefault();
        var elegido=botones[indiceActivo]||botones[0];if(elegido)elegir(elegido.dataset.valor);
      }
    });
    flecha.addEventListener("click",function(){if(lista.hidden)abrir();else cerrar()});
    select.addEventListener("change",function(){buscando=false;sincronizar()});
    select.addEventListener("focus",function(){campo.focus()});
    document.addEventListener("pointerdown",function(e){if(!caja.contains(e.target)&&!lista.hidden)cerrar()});
    new MutationObserver(function(){sincronizar();if(!lista.hidden)pintar(campo.value)}).observe(select,{
      childList:true,subtree:true,attributes:true,attributeFilter:["disabled","selected","label"]
    });
    var propiedad=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,"value");
    Object.defineProperty(select,"value",{
      configurable:true,get:function(){return propiedad.get.call(select)},
      set:function(valor){propiedad.set.call(select,valor);sincronizar()}
    });
    sincronizar();
  }
  function iniciar(){document.querySelectorAll("select:not([data-buscable-listo])").forEach(mejorar)}
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",iniciar);
  else iniciar();
  new MutationObserver(iniciar).observe(document.documentElement,{childList:true,subtree:true});
})();
