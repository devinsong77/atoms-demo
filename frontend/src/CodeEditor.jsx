import React, { useEffect, useMemo, useRef, useState } from 'react'
import Editor, { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import JsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker'
import CssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker'
import TsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker'
import prettier from 'prettier/standalone'
import babelPlugin from 'prettier/plugins/babel'
import typescriptPlugin from 'prettier/plugins/typescript'
import estreePlugin from 'prettier/plugins/estree'
import htmlPlugin from 'prettier/plugins/html'
import postcssPlugin from 'prettier/plugins/postcss'
import markdownPlugin from 'prettier/plugins/markdown'
import { Braces, Check, CircleAlert, LoaderCircle, Save } from 'lucide-react'
import { useUi } from './i18n'

loader.config({ monaco })
self.MonacoEnvironment = { getWorker: (_, label) => {
  if (label === 'json') return new JsonWorker()
  if (['css','scss','less'].includes(label)) return new CssWorker()
  if (['html','handlebars','razor'].includes(label)) return new HtmlWorker()
  if (['typescript','javascript'].includes(label)) return new TsWorker()
  return new EditorWorker()
} }

const languageFor = path => {
  const ext = path.split('.').pop()?.toLowerCase()
  return ({ py:'python', js:'javascript', jsx:'javascript', ts:'typescript', tsx:'typescript', css:'css', scss:'scss', html:'html', json:'json', md:'markdown' })[ext] || 'plaintext'
}

async function formatWeb(path, content) {
  const ext = path.split('.').pop()?.toLowerCase()
  const options = { tabWidth: 2, printWidth: 100, semi: false, singleQuote: true }
  if (['js','jsx'].includes(ext)) return prettier.format(content, { ...options, parser:'babel', plugins:[babelPlugin, estreePlugin] })
  if (['ts','tsx'].includes(ext)) return prettier.format(content, { ...options, parser:'typescript', plugins:[typescriptPlugin, estreePlugin] })
  if (['css','scss'].includes(ext)) return prettier.format(content, { ...options, parser:ext, plugins:[postcssPlugin] })
  if (ext === 'html') return prettier.format(content, { ...options, parser:'html', plugins:[htmlPlugin] })
  if (ext === 'json') return prettier.format(content, { ...options, parser:'json', plugins:[babelPlugin, estreePlugin] })
  if (ext === 'md') return prettier.format(content, { ...options, parser:'markdown', plugins:[markdownPlugin] })
  return content
}

export default function CodeEditor({ path, value, revision, onChange, onSave, onFormatPython, saving, saveResult, dirty, readOnly }) {
  const { theme, t } = useUi()
  const editorRef=useRef(null), monacoRef=useRef(null), valueRef=useRef(value)
  const [diagnostics,setDiagnostics]=useState([]), [formatting,setFormatting]=useState(false)
  const language = useMemo(() => languageFor(path), [path])
  useEffect(()=>{valueRef.current=value},[value])
  useEffect(()=>setDiagnostics([]),[path,revision])
  const markerFor=error=>{
    const match=String(error.message).match(/\((\d+):(\d+)\)/)
    const start=error.loc?.start||error.location?.start||{}
    const line=start.line||Number(match?.[1])||1, column=start.column||Number(match?.[2])||1
    return {severity:monaco.MarkerSeverity.Error,message:error.message||String(error),startLineNumber:line,startColumn:column,endLineNumber:line,endColumn:column+1}
  }
  const save=async()=>{
    if(!path||readOnly||saving)return
    setFormatting(true)
    const model=editorRef.current?.getModel()
    try{
      const formatted=language==='python'?await onFormatPython(valueRef.current):await formatWeb(path,valueRef.current)
      if(model)monacoRef.current.editor.setModelMarkers(model,'formatter',[])
      onChange(formatted);valueRef.current=formatted
      await onSave(formatted)
    }catch(error){
      if(model){const marker=markerFor(error);monacoRef.current.editor.setModelMarkers(model,'formatter',[marker]);editorRef.current.revealLineInCenter(marker.startLineNumber);editorRef.current.focus()}
      setDiagnostics(items=>[...items.filter(x=>x.owner!=='formatter'),{message:error.message,severity:8,owner:'formatter'}])
    }finally{setFormatting(false)}
  }
  const mount=(editor,monacoApi)=>{editorRef.current=editor;monacoRef.current=monacoApi;editor.addCommand(monacoApi.KeyMod.CtrlCmd|monacoApi.KeyCode.KeyS,save)}
  return <div className="monaco-shell"><div className="editor-chrome"><div className="editor-toolbar"><span><Braces size={16}/><b>{path||t('noFile')}</b>{dirty&&<i>{t('unsaved')}</i>}{saveResult?.saved&&!dirty&&<em><Check size={13}/>{t('saved')}</em>}{diagnostics.length>0&&<strong className="diagnostic-count"><CircleAlert size={13}/>{diagnostics.length}</strong>}</span><div><small>{language==='python'?'Black':'Prettier'} · diagnostics</small><button className="editor-save" onClick={save} disabled={!path||saving||formatting||readOnly||!dirty}>{saving||formatting?<LoaderCircle className="spin" size={15}/>:<Save size={15}/>} {t('save')}</button></div></div>{saveResult&&!saveResult.checks?.ok&&<div className="editor-alert warning"><CircleAlert size={14}/>{(saveResult.checks?.errors||[]).length} {t('checksFailed')}</div>}</div><Editor key={`${path}:${revision}`} path={path} language={language} value={value} onChange={next=>onChange(next??'')} onMount={mount} onValidate={markers=>setDiagnostics(markers)} theme={theme==='dark'?'vs-dark':'vs'} options={{readOnly,fontSize:14,lineHeight:22,fontFamily:"'Cascadia Code','SFMono-Regular',Consolas,monospace",fontLigatures:true,minimap:{enabled:true},wordWrap:'on',automaticLayout:true,scrollBeyondLastLine:false,padding:{top:14},tabSize:2,renderValidationDecorations:'on',glyphMargin:true,renderWhitespace:'selection'}}/></div>
}
