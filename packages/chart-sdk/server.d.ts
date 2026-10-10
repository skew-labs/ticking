/** The backend entry point has no DOM, React or Node-type-package dependency. */
export interface LicenseFetchOptions {method:'POST';headers:Record<string,string>;body:string}
export interface LicenseFetchResponse {ok:boolean;json():Promise<unknown>}
export interface LicenseBrokerOptions {endpoint:string;apiKey:string;origin:string;fetcher?:(input:string,options:LicenseFetchOptions)=>Promise<LicenseFetchResponse>}
export interface LicenseBroker {license():Promise<{license:string}>;clear():void}
export function createLicenseBroker(options:LicenseBrokerOptions):LicenseBroker;
