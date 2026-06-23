// Octane classifier — interprets rules.json.
// Exports a single global function: window.makeOctane(rules) -> function(brand, model, trim, eng, nefach)
// The returned function is a drop-in replacement for the inline octane()/petrolOctane() implementations.
(function(){
  function likeRegex(pat){
    const esc = pat.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp("^" + esc.replace(/%/g, ".*").replace(/_/g, ".") + "$", "i");
  }
  function like(s, pat){ return likeRegex(pat).test(s); }
  function anyLike(s, pats){ return pats.some(p => like(s, p)); }

  function check(cond, ctx){
    if(!cond) return true;
    if(cond.brand && !cond.brand.some(p => ctx.brand.startsWith(p))) return false;
    if(cond.name  && !anyLike(ctx.name, cond.name))  return false;
    if(cond.trim  && !anyLike(ctx.trim, cond.trim))  return false;
    if(cond.name_or_trim && !cond.name_or_trim.some(p => like(ctx.name,p) || like(ctx.trim,p))) return false;
    if(cond.name_not && anyLike(ctx.name, cond.name_not)) return false;
    if(cond.trim_not && anyLike(ctx.trim, cond.trim_not)) return false;
    if(cond.name_or_trim_not && cond.name_or_trim_not.some(p => like(ctx.name,p) || like(ctx.trim,p))) return false;
    if(cond.engine_in && !cond.engine_in.includes(ctx.eng)) return false;
    if(cond.engine_eq !== undefined && ctx.eng !== cond.engine_eq) return false;
    if(cond.engine_starts_with && !cond.engine_starts_with.some(p => ctx.eng.startsWith(p))) return false;
    if(cond.engine_set && !ctx.sets[cond.engine_set].has(ctx.eng)) return false;
    if(cond.nefach_between){
      const n = +ctx.nefach;
      if(!(n >= cond.nefach_between[0] && n <= cond.nefach_between[1])) return false;
    }
    if(cond.any && !cond.any.some(sub => check(sub, ctx))) return false;
    if(cond.all && !cond.all.every(sub => check(sub, ctx))) return false;
    return true;
  }
  function evalThen(then, ctx){
    if(then == null) return null;
    if(typeof then === "string") return then;             // direct result
    if(Array.isArray(then)){                              // nested sub-rules
      for(const sub of then){
        if(!check(sub.when, ctx)) continue;
        const r = evalThen(sub.then, ctx);
        if(r) return r;
      }
    }
    return null;
  }

  window.makeOctane = function(rules){
    const sets = {};
    for(const [k, arr] of Object.entries(rules.engine_sets || {})) sets[k] = new Set(arr);
    const aliases = rules.brand_aliases || {};
    const aliasKeys = Object.keys(aliases);
    const cosmName = (rules.cosmetic && rules.cosmetic.name) || [];
    const cosmTrim = (rules.cosmetic && rules.cosmetic.trim) || [];

    return function octane(brand, model, trim, eng, nefach){
      // brand alias resolution (cryptic Hebrew tozerets -> canonical)
      let b = brand || "";
      for(const k of aliasKeys) if(b.startsWith(k)){ b = aliases[k]; break; }

      const ctx = {
        brand: b,
        name:   (model||"").toUpperCase(),
        trim:   (trim||"").toUpperCase(),
        eng:    (eng||"").toUpperCase().replace(/[\s.\-]/g,""),
        nefach: nefach || "",
        sets
      };

      const cosmetic = anyLike(ctx.name, cosmName) || anyLike(ctx.trim, cosmTrim);

      for(const rule of rules.rules || []){
        if(rule.skip_if_cosmetic && cosmetic) continue;
        if(!check(rule.when, ctx)) continue;
        const r = evalThen(rule.then, ctx);
        if(r) return r;
      }
      return rules.default || "95";
    };
  };
})();
